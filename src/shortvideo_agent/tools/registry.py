"""工具注册表与执行器。

设计要点：
- 每个工具是 `handler(state, args) -> ToolResult`，可以读当前 run state；
- 工具调用前做**阶段白名单**校验：模型在错误阶段选错工具时，返回一句可读的
  observation 让它自我纠正，而不是让流程崩溃；
- 工具内部异常统一转成 `ok=False` 的观察结果，模型能看到失败并决定重试/换参/放弃；
- 支持故障注入（`--fault`），用于可复现地验证“工具失败”验收点。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..models import ToolResult

Handler = Callable[[dict[str, Any], dict[str, Any]], ToolResult]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    terminal: bool = False   # 终止类工具：调用后直接结束图


TOOL_REGISTRY: dict[str, ToolSpec] = {}

# 阶段 → 允许的工具。这是“执行骨架”的一部分，避免模型在错误时机调错工具。
PHASE_TOOLS: dict[str, set[str]] = {
    "research": {"search_materials", "request_clarification", "report_unfixable"},
    "draft": {"submit_script", "request_clarification", "report_unfixable"},
    "verify": {
        "estimate_duration",
        "check_rules",
        "submit_script",
        "finalize_script",
        "request_clarification",
        "report_unfixable",
    },
}

REASON_PROPERTY = {
    "type": "string",
    "description": "一句话说明本次动作的决策依据（结论式陈述，不要写思考过程）",
}


def register(
    name: str,
    description: str,
    parameters: dict[str, Any],
    terminal: bool = False,
) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        props = dict(parameters.get("properties", {}))
        props["reason"] = REASON_PROPERTY
        required = list(parameters.get("required", []))
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": props, "required": required},
            handler=fn,
            terminal=terminal,
        )
        return fn

    return decorator


def tool_schemas(phase: str) -> list[dict[str, Any]]:
    """返回当前阶段可用工具的 JSON Schema（直接喂给模型的 tools 字段）。"""
    allowed = PHASE_TOOLS.get(phase, set())
    return [
        {"type": "function", "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }}
        for spec in TOOL_REGISTRY.values()
        if spec.name in allowed
    ]


def describe_tools(phase: str) -> str:
    allowed = PHASE_TOOLS.get(phase, set())
    return "\n".join(
        f"- {spec.name}: {spec.description}"
        for spec in TOOL_REGISTRY.values()
        if spec.name in allowed
    )


def _fault_for(state: dict[str, Any], name: str) -> str | None:
    faults = state.get("faults") or {}
    fault = faults.get(name)
    if not fault:
        return None
    fired = dict(state.get("fault_fired") or {})
    if fault == "error_always":
        return "error_always"
    if fault == "error_once" and not fired.get(name):
        return "error_once"
    return None


def execute_tool(state: dict[str, Any], name: str, args: dict[str, Any] | None) -> ToolResult:
    args = dict(args or {})
    phase = state.get("phase", "research")
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return ToolResult(
            ok=False,
            tool=name,
            summary=f"未知工具 `{name}`，当前可用：{', '.join(sorted(PHASE_TOOLS.get(phase, set())))}。",
            error="unknown_tool",
        )
    if name not in PHASE_TOOLS.get(phase, set()):
        return ToolResult(
            ok=False,
            tool=name,
            summary=(
                f"当前阶段 `{phase}` 不允许调用 `{name}`。"
                f"可用：{', '.join(sorted(PHASE_TOOLS.get(phase, set())))}。"
            ),
            error="tool_not_allowed_in_phase",
        )

    fault = _fault_for(state, name)
    if fault:
        fired = dict(state.get("fault_fired") or {})
        fired[name] = True
        return ToolResult(
            ok=False,
            tool=name,
            summary=f"工具 `{name}` 执行失败（注入故障 {fault}）：上游服务超时。",
            error="injected_tool_failure",
            state_patch={"fault_fired": fired},
        )

    try:
        result = spec.handler(state, args)
    except Exception as exc:  # noqa: BLE001 - 工具异常必须被观察，不能中断流程
        return ToolResult(
            ok=False,
            tool=name,
            summary=f"工具 `{name}` 抛出异常：{type(exc).__name__}: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )
    if not isinstance(result, ToolResult):
        raise TypeError(f"工具 {name} 返回值必须是 ToolResult")
    return result
