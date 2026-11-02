"""稿件相关的工具：提交、终稿、请求补充、声明不可修。

这里的守卫是“工程护栏”，不替代模型决策：
- submit_script 只校验结构与引用合法性；
- finalize_script 拒绝在未做时长/规则检查时提交终稿（防止模型自报“已达标”）。
"""

from __future__ import annotations

from .. import config
from ..models import CheckReport, DurationReport, Script, Status, ToolResult
from .registry import register

SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "shot_id": {"type": "integer"},
        "visual": {"type": "string", "description": "画面/运镜描述"},
        "narration": {"type": "string", "description": "旁白，同时也是字幕文本"},
        "fact_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该镜引用的素材 ID，必须来自 search_materials 的返回结果；无事实依据时留空",
        },
    },
    "required": ["shot_id", "visual", "narration", "fact_refs"],
}

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "标题"},
        "platform": {"type": "string", "description": "平台标识，如 douyin"},
        "target_seconds": {"type": "integer", "description": "目标时长（秒）"},
        "shots": {"type": "array", "items": SHOT_SCHEMA, "description": "分镜列表，按顺序排列"},
        "disclaimer": {"type": "string", "description": "免责声明，需包含品牌要求原文"},
    },
    "required": ["title", "platform", "target_seconds", "shots"],
}


@register(
    name="submit_script",
    description=(
        "提交或修改稿件。提交后时长与规则检查结果会失效，必须重新调用 "
        "estimate_duration 与 check_rules。旁白中的每个产品事实都必须绑定 fact_refs。"
    ),
    parameters={
        "type": "object",
        "properties": {"script": SCRIPT_SCHEMA},
        "required": ["script"],
    },
)
def submit_script_handler(state: dict, args: dict) -> ToolResult:
    raw = args.get("script") or {}
    brief = state.get("brief") or {}
    try:
        script = Script(**raw)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            ok=False, tool="submit_script",
            summary=f"稿件结构不合法，请修正后重试：{exc}", error="invalid_script",
        )

    if script.target_seconds != brief.get("target_seconds"):
        script.target_seconds = int(brief.get("target_seconds") or script.target_seconds)
    if brief.get("platform") and script.platform != brief["platform"]:
        script.platform = brief["platform"]

    known = {m["material_id"] for m in (state.get("materials") or [])}
    bad = sorted({r for s in script.shots for r in s.fact_refs if r not in known})
    if bad:
        return ToolResult(
            ok=False, tool="submit_script",
            summary=(
                f"稿件引用了未检索到的素材 ID：{bad}。只能引用已返回的：{sorted(known)}，"
                "或删除这些引用并把该镜改为不含产品事实的表达。"
            ),
            error="invalid_fact_refs",
        )

    previous = state.get("script")
    rounds = int(state.get("repair_rounds") or 0)
    if previous is not None:
        rounds += 1
        if rounds > config.MAX_REPAIR_ROUNDS:
            return ToolResult(
                ok=False, tool="submit_script",
                summary=(
                    f"已达修正轮数上限（{config.MAX_REPAIR_ROUNDS} 轮），不再接受新版本。"
                    "请调用 report_unfixable 说明无法满足的具体原因，或调用 request_clarification 请求用户放宽要求。"
                ),
                error="repair_limit_reached",
            )

    chars = sum(len(s.narration) for s in script.shots)
    patch = {
        "script": script.model_dump(),
        "duration": None,
        "check": None,
        "phase": "verify",
        "repair_rounds": rounds,
    }
    note = "已接收初稿" if previous is None else f"已接收第 {rounds} 版修改稿"
    return ToolResult(
        ok=True, tool="submit_script",
        summary=(
            f"{note}（{len(script.shots)} 镜，旁白 {chars} 字）。"
            "旧的时长与规则结论已失效，请先调用 estimate_duration，再调用 check_rules。"
        ),
        data={"shots": len(script.shots), "narration_chars": chars, "repair_rounds": rounds},
        state_patch=patch,
    )


@register(
    name="finalize_script",
    description="提交终稿。只有当 estimate_duration 在容差内且 check_rules 无 error 时才会成功。",
    parameters={
        "type": "object",
        "properties": {"note": {"type": "string", "description": "可选的交付说明"}},
        "required": [],
    },
    terminal=True,
)
def finalize_script_handler(state: dict, args: dict) -> ToolResult:
    script_raw = state.get("script")
    duration_raw = state.get("duration")
    check_raw = state.get("check")

    missing = []
    if not script_raw:
        missing.append("稿件")
    if not duration_raw:
        missing.append("时长估算（estimate_duration）")
    if not check_raw:
        missing.append("规则检查（check_rules）")
    if missing:
        return ToolResult(
            ok=False, tool="finalize_script",
            summary=f"无法提交终稿：缺少 {'、'.join(missing)}。请先补齐对应工具调用。",
            error="precheck_missing",
        )

    duration = DurationReport(**duration_raw)
    check = CheckReport(**check_raw)
    if not duration.within_tolerance:
        return ToolResult(
            ok=False, tool="finalize_script",
            summary=(
                f"无法提交终稿：时长仍超出 {duration.over_by}s（{duration.total_seconds}s / "
                f"{duration.target_seconds}s）。请继续压缩后重新检查。"
            ),
            error="duration_overrun",
        )
    if not check.ok:
        codes = [v.code for v in check.violations if v.severity == "error"]
        return ToolResult(
            ok=False, tool="finalize_script",
            summary=f"无法提交终稿：规则检查仍有 error（{codes}）。请修正后重新检查。",
            error="rules_not_passed",
        )

    warnings = [v.message for v in check.violations if v.severity == "warn"]
    status = Status.completed_with_warnings if warnings else Status.completed
    risks = ["时长按“字数/语速”估算，未含停顿与画面节奏，实际成片可能有 ±10% 偏差；"]
    if warnings:
        risks.append("规则检查存在提示项：" + "; ".join(warnings))

    return ToolResult(
        ok=True, tool="finalize_script",
        summary=(
            f"终稿已确认：{duration.total_seconds}s / 目标 {duration.target_seconds}s，"
            f"规则检查通过，引用素材 {check.cited_materials}。"
        ),
        data={"status": status.value, "warnings": warnings},
        state_patch={
            "status": status.value,
            "risks": list(state.get("risks") or []) + risks,
            "result_messages": list(state.get("result_messages") or [])
            + [f"终稿完成，预计总时长 {duration.total_seconds} 秒。"],
        },
    )


@register(
    name="request_clarification",
    description=(
        "当缺少关键信息，或多轮改写检索后仍无素材依据时，向用户提出补充问题并结束本次任务。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array", "items": {"type": "string"},
                "description": "需要用户补充的信息，每条一句",
            },
            "reason": {"type": "string", "description": "为什么必须补充"},
        },
        "required": ["questions"],
    },
    terminal=True,
)
def request_clarification_handler(state: dict, args: dict) -> ToolResult:
    questions = [q for q in (args.get("questions") or []) if q]
    if not questions:
        questions = ["请补充主题、平台与目标时长。"]
    reason = args.get("reason") or "当前信息不足以生成有事实依据的脚本。"
    return ToolResult(
        ok=True, tool="request_clarification",
        summary="已向用户发起补充请求，任务以 needs_input 结束。",
        data={"questions": questions, "reason": reason},
        state_patch={
            "status": Status.needs_input.value,
            "result_messages": list(state.get("result_messages") or [])
            + [f"需要你补充信息：{reason}"] + [f"- {q}" for q in questions],
        },
    )


@register(
    name="report_unfixable",
    description=(
        "明确声明无法满足约束（例如时长压到目标值后无法保留必要信息）。"
        "调用后任务以 failed_unfixable 结束，并附带最接近目标的方案与原因。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "无法满足的具体原因"},
            "best_effort": {"type": "string", "description": "当前最接近目标的方案说明"},
        },
        "required": ["reason"],
    },
    terminal=True,
)
def report_unfixable_handler(state: dict, args: dict) -> ToolResult:
    reason = args.get("reason") or "多轮修正后仍无法同时满足时长与规则约束。"
    best = args.get("best_effort") or ""
    duration = state.get("duration") or {}
    detail = ""
    if duration:
        detail = f"（当前最接近方案：{duration.get('total_seconds')}s / 目标 {duration.get('target_seconds')}s）"
    return ToolResult(
        ok=True, tool="report_unfixable",
        summary=f"已声明无法满足：{reason}{detail}",
        data={"reason": reason, "best_effort": best},
        state_patch={
            "status": Status.failed_unfixable.value,
            "result_messages": list(state.get("result_messages") or [])
            + [f"无法满足要求：{reason}{detail}"]
            + ([best] if best else []),
        },
    )
