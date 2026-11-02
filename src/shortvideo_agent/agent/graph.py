"""LangGraph 决策图：agent（模型选动作）→ tools（执行工具）→ agent。

只做三件事，其余交给模型：
1. 阶段白名单（骨架）：限制每个阶段可选工具，避免流程被拉乱；
2. 上限与终止（护栏）：步数、工具调用、改稿轮数到顶即返回明确未完成状态；
3. trace（可审计）：记录 决策 → 工具参数 → 返回摘要 → 反应依据。

下一次是继续调工具还是结束，由模型的输出决定，而不是代码里的固定分支。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from .. import config
from ..llm.base import LLMClient, LLMError
from ..models import (
    Brief,
    CheckReport,
    DurationReport,
    Script,
    ScriptResult,
    Status,
)
from ..tools import PHASE_TOOLS, execute_tool


class AgentState(TypedDict, total=False):
    case_id: str
    brief: dict[str, Any]
    phase: str
    missing_fields: list[str]
    materials: list[dict]
    search_queries: list[str]
    search_attempts: int
    script: dict | None
    duration: dict | None
    check: dict | None
    repair_rounds: int
    steps: int
    tool_calls: int
    invalid_outputs: int
    faults: dict[str, str]
    fault_fired: dict[str, bool]
    mock_draft_ratio: float | None
    pending: dict | None
    observations: Annotated[list[dict], operator.add]
    trace: Annotated[list[dict], operator.add]
    status: str | None
    risks: list[str]
    result_messages: list[str]


def _limit_message(state: dict[str, Any], reason: str) -> list[str]:
    """达到上限时必须说明“做到哪一步了”，不能只抛一句失败。"""
    done = []
    done.append("已有稿件" if state.get("script") else "无可用稿件")
    if state.get("duration"):
        d = state["duration"]
        done.append(f"最近一次估算 {d['total_seconds']}s / 目标 {d['target_seconds']}s")
    if state.get("check"):
        done.append("规则检查已执行" if state["check"].get("ok") is False else "规则检查通过")
    return [f"{reason}，任务未完成。当前进度：{'；'.join(done)}。"]


def _trace_model(state: dict[str, Any], step: int, name: str, args: dict, reason: str) -> dict:
    observations = state.get("observations") or []
    reacts = observations[-1].get("summary", "")[:110] if observations else ""
    return {
        "step": step,
        "phase": state.get("phase"),
        "actor": "model",
        "tool": name,
        # 基于刚才的观察选择的下一步动作
        "next_action": name,
        "args": {k: v for k, v in args.items() if k != "reason"},
        "decision_basis": reason or "（模型未填写决策依据）",
        "reacts_to": reacts,
    }


def _trace_tool(state: dict[str, Any], step: int, result) -> dict:
    return {
        "step": step,
        "phase": state.get("phase"),
        "actor": "tool",
        "tool": result.tool,
        "ok": result.ok,
        "error": result.error,
        "result_summary": result.summary[:400],
    }


def build_graph(client: LLMClient):
    def agent_node(state: AgentState) -> dict:
        steps = int(state.get("steps") or 0) + 1
        if steps > config.MAX_STEPS:
            return {
                "steps": steps,
                "status": Status.incomplete_limit_reached.value,
                "result_messages": _limit_message(state, f"达到步数上限 {config.MAX_STEPS} 步"),
            }

        phase = state.get("phase", "research")
        available = sorted(PHASE_TOOLS.get(phase, set()))
        try:
            invocation = client.decide(state, available)
        except LLMError as exc:
            return {
                "steps": steps,
                "status": Status.failed_tool_error.value,
                "result_messages": [f"模型不可用：{exc}"],
            }

        if invocation is None:
            invalid = int(state.get("invalid_outputs") or 0) + 1
            return {
                "steps": steps,
                "invalid_outputs": invalid,
                "observations": [{
                    "tool": "_protocol",
                    "ok": False,
                    "summary": (
                        "你没有返回工具调用。必须通过工具推进任务，"
                        f"当前可用：{', '.join(available)}。"
                    ),
                }],
                "trace": [{
                    "step": steps, "phase": phase, "actor": "model",
                    "tool": None, "args": {},
                    "decision_basis": "模型未按协议返回工具调用，要求其重试。",
                }],
            }

        args = invocation.args or {}
        return {
            "steps": steps,
            "pending": {"name": invocation.name, "args": args},
            "trace": [_trace_model(state, steps, invocation.name, args, invocation.reason)],
        }

    def tools_node(state: AgentState) -> dict:
        tool_calls = int(state.get("tool_calls") or 0) + 1
        if tool_calls > config.MAX_TOOL_CALLS:
            return {
                "tool_calls": tool_calls,
                "pending": None,
                "status": Status.incomplete_limit_reached.value,
                "result_messages": _limit_message(
                    state, f"达到工具调用上限 {config.MAX_TOOL_CALLS} 次"
                ),
            }

        pending = state.get("pending") or {}
        result = execute_tool(state, pending.get("name", ""), pending.get("args") or {})

        patch: dict[str, Any] = dict(result.state_patch)
        patch.update({
            "tool_calls": tool_calls,
            "pending": None,
            "observations": [{
                "tool": result.tool,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "data": result.data,
            }],
            "trace": [_trace_tool(state, int(state.get("steps") or 0), result)],
        })
        return patch

    def abort_node(state: AgentState) -> dict:
        if state.get("status"):
            return {}
        reason = "模型连续未能给出合规的工具调用"
        if int(state.get("tool_calls") or 0) > config.MAX_TOOL_CALLS:
            reason = f"达到工具调用上限 {config.MAX_TOOL_CALLS} 次"
        elif int(state.get("steps") or 0) > config.MAX_STEPS:
            reason = f"达到步数上限 {config.MAX_STEPS} 步"
        return {
            "status": Status.incomplete_limit_reached.value,
            "result_messages": _limit_message(state, reason),
        }

    def route_after_agent(state: AgentState) -> str:
        if state.get("status"):
            return END
        if state.get("pending"):
            return "tools"
        # 模型没按协议返回工具调用：先回灌一次提示让它纠正，连续两次才放弃
        if int(state.get("invalid_outputs") or 0) < 2:
            return "agent"
        return "abort"

    def route_after_tools(state: AgentState) -> str:
        if state.get("status"):
            return END
        if int(state.get("tool_calls") or 0) > config.MAX_TOOL_CALLS:
            return "abort"
        return "agent"

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("abort", abort_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "agent": "agent", "abort": "abort", END: END},
    )
    builder.add_conditional_edges(
        "tools", route_after_tools, {"agent": "agent", "abort": "abort", END: END}
    )
    builder.add_edge("abort", END)
    return builder.compile()


def initial_state(case: dict[str, Any]) -> AgentState:
    brief = case.get("brief") or {}
    parsed = Brief(**brief)
    return AgentState(
        case_id=case.get("case_id") or "case",
        brief=brief,
        phase="research",
        missing_fields=parsed.missing_required(),
        materials=[],
        search_queries=[],
        search_attempts=0,
        script=None,
        duration=None,
        check=None,
        repair_rounds=0,
        steps=0,
        tool_calls=0,
        invalid_outputs=0,
        faults=case.get("faults") or {},
        fault_fired={},
        mock_draft_ratio=case.get("mock_draft_ratio"),
        pending=None,
        observations=[],
        trace=[],
        status=None,
        risks=[],
        result_messages=[],
    )


def build_result(state: dict[str, Any], mode: str, llm_info: str | None = None) -> ScriptResult:
    script = Script(**state["script"]) if state.get("script") else None
    duration = DurationReport(**state["duration"]) if state.get("duration") else None
    check = CheckReport(**state["check"]) if state.get("check") else None

    fact_map: dict[str, list[str]] = {}
    if script:
        known = {m["material_id"]: m for m in (state.get("materials") or [])}
        for shot in script.shots:
            for ref in shot.fact_refs:
                if ref in known and ref not in fact_map:
                    fact_map[ref] = [known[ref]["snippet"]]

    status = state.get("status")
    if not status:
        status = (
            Status.completed.value
            if check and check.ok and duration and duration.within_tolerance
            else Status.incomplete_limit_reached.value
        )

    messages = list(state.get("result_messages") or [])
    if status in (Status.completed.value, Status.completed_with_warnings.value):
        messages.append(
            f"脚本已完成：{len(script.shots) if script else 0} 个分镜，预计 "
            f"{duration.total_seconds if duration else '-'} 秒。"
        )
    if not messages:
        messages.append(f"任务结束，状态：{status}。")

    return ScriptResult(
        case_id=state.get("case_id") or "case",
        mode=mode,
        llm_info=llm_info,
        status=Status(status),
        script=script,
        estimated_total_seconds=duration.total_seconds if duration else None,
        target_seconds=(state.get("brief") or {}).get("target_seconds"),
        fact_map=fact_map,
        check=check,
        duration=duration,
        risks=list(state.get("risks") or []),
        messages=messages,
        steps=int(state.get("steps") or 0),
        tool_calls=int(state.get("tool_calls") or 0),
    )


def run_case(
    case: dict[str, Any],
    client: LLMClient,
    on_step: Callable[[dict], None] | None = None,
) -> tuple[ScriptResult, list[dict], dict]:
    """跑一个用例，返回（结果, trace, 最终 state）。

    on_step 用于交互模式实时展示每一步；传 None 时走 invoke，行为与之前一致。
    """
    state = initial_state(case)
    app = build_graph(client)
    cfg = {"recursion_limit": config.RECURSION_LIMIT}
    final: dict[str, Any] = dict(state)
    try:
        if on_step is None:
            final = app.invoke(state, config=cfg)
        else:
            seen = 0
            for chunk in app.stream(state, config=cfg, stream_mode="values"):
                final = chunk
                trace_now = chunk.get("trace") or []
                for entry in trace_now[seen:]:
                    on_step(entry)
                seen = len(trace_now)
    except GraphRecursionError:
        fallback = dict(final)
        fallback.update({
            "status": Status.incomplete_limit_reached.value,
            "result_messages": _limit_message(
                final, f"达到图递归上限 {config.RECURSION_LIMIT}"
            ),
        })
        final = fallback
    return build_result(final, client.mode, client.describe()), list(final.get("trace") or []), dict(final)
