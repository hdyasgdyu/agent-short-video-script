"""Pydantic 契约：输入、工具返回值、最终交付物。

这些模型同时承担三个职责：
1. 约束模型输出（function calling 参数校验）；
2. 定义 trace / 结果 JSON 的结构；
3. 作为测试的断言对象。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 输入
class Brief(BaseModel):
    """用户简报。hard-required 字段缺失即无法开工。"""

    topic: str | None = None
    audience: str | None = None
    platform: str | None = None
    target_seconds: int | None = None
    style: str | None = None
    product_ids: list[str] = Field(default_factory=list)

    HARD_REQUIRED: ClassVar[tuple[str, ...]] = ("topic", "platform", "target_seconds")

    def missing_required(self) -> list[str]:
        return [f for f in self.HARD_REQUIRED if not getattr(self, f)]

    def missing_optional(self) -> list[str]:
        return [f for f in ("audience", "style") if not getattr(self, f)]


# ---------------------------------------------------------------- 状态
class Status(str, Enum):
    completed = "completed"
    completed_with_warnings = "completed_with_warnings"
    needs_input = "needs_input"                      # 缺关键信息 / 无素材，请求用户补充
    failed_unfixable = "failed_unfixable"            # 反复修正仍不达标，明确失败
    failed_tool_error = "failed_tool_error"          # 关键工具不可用且无法降级
    incomplete_limit_reached = "incomplete_limit_reached"  # 达到步数/调用上限


# ---------------------------------------------------------------- 素材与脚本
class MaterialHit(BaseModel):
    material_id: str
    product_id: str
    product_name: str
    snippet: str
    score: float
    source_file: str


class Shot(BaseModel):
    shot_id: int
    visual: str = ""
    narration: str = ""
    duration_est: float = 0.0
    fact_refs: list[str] = Field(default_factory=list)


class Script(BaseModel):
    title: str
    platform: str
    target_seconds: int
    shots: list[Shot]
    disclaimer: str | None = None

    def narration_text(self) -> str:
        return "".join(s.narration for s in self.shots)


# ---------------------------------------------------------------- 工具返回
class DurationReport(BaseModel):
    cps: float
    per_shot: dict[str, float]
    total_seconds: float
    target_seconds: int
    over_by: float
    within_tolerance: bool


class Violation(BaseModel):
    code: str
    severity: Literal["error", "warn"] = "error"
    message: str
    locator: str | None = None
    fix_hint: str | None = None


class CheckReport(BaseModel):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)
    checked_shots: int = 0
    cited_materials: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    """工具的观察结果。每次调用都会被写入 trace 并回灌给模型。"""

    ok: bool
    tool: str
    summary: str            # 给模型看的简短观察
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    state_patch: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- 交付物
class ScriptResult(BaseModel):
    case_id: str
    mode: str
    llm_info: str | None = None
    status: Status
    script: Script | None = None
    estimated_total_seconds: float | None = None
    target_seconds: int | None = None
    fact_map: dict[str, list[str]] = Field(default_factory=dict)
    check: CheckReport | None = None
    duration: DurationReport | None = None
    risks: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    trace_file: str | None = None
