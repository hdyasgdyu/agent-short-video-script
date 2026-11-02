"""规则检查工具。

承担两类职责，对应题目的两条硬约束：
1. 形式合规：时长、镜头数、标题字数、禁用词、免责声明；
2. **事实合规**：每条带断言/数字的旁白必须绑定素材 ID，且素材原文里确实包含该断言。
   这是防“编造产品事实”和“无数据却写效率提升 80%”的核心闸门。
"""

from __future__ import annotations

import re

from .. import config
from ..models import CheckReport, DurationReport, Script, ToolResult, Violation
from .registry import register
from .search_materials import score

# 只把“数字 + 量纲”视为数据断言，避免把产品型号（S2、X1、P02）误判成编造数据
_UNIT = r"(?:%|％|小时|分钟|秒钟|秒|千克|公斤|kg|克|mAh|毫安时|毫安|分贝|dB|瓦|W|英寸|寸|倍|万|亿|p)"
_NUMBER = re.compile(rf"(\d+(?:\.\d+)?)\s*{_UNIT}", re.IGNORECASE)
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*[%％]")


def _snippets_by_id(materials: list[dict]) -> dict[str, dict]:
    return {m["material_id"]: m for m in materials}


def check_script(
    script: Script,
    materials: list[dict],
    rules: dict,
    duration: DurationReport | None,
) -> CheckReport:
    violations: list[Violation] = []
    known = _snippets_by_id(materials)
    cited: list[str] = []

    # 1) 时长
    if duration is None:
        violations.append(Violation(
            code="MISSING_DURATION_CHECK",
            message="尚未调用 estimate_duration，无法确认是否满足时长限制。",
            fix_hint="先调用 estimate_duration，再调用 check_rules。",
        ))
    elif not duration.within_tolerance:
        violations.append(Violation(
            code="DURATION_OVERRUN",
            message=(
                f"估算时长 {duration.total_seconds}s 超出目标 {duration.target_seconds}s 共 "
                f"{duration.over_by}s（容差 {config.DURATION_TOLERANCE_SECONDS}s）。"
            ),
            locator="duration",
            fix_hint="压缩旁白字数（优先删掉非事实性铺垫）或减少镜头，重新提交后再次估算。",
        ))

    # 2) 结构
    min_shots = int(rules.get("min_shots", 3))
    if len(script.shots) < min_shots:
        violations.append(Violation(
            code="TOO_FEW_SHOTS",
            message=f"分镜数量 {len(script.shots)} 少于平台要求的最少 {min_shots} 镜。",
            fix_hint="补充分镜，或合并信息密度过低的内容。",
        ))

    title_limit = int(rules.get("title_max_chars", 20))
    if len(script.title) > title_limit:
        violations.append(Violation(
            code="TITLE_TOO_LONG",
            message=f"标题 {len(script.title)} 字，超过上限 {title_limit} 字。",
            locator="title",
            fix_hint="精简标题。",
        ))

    # 3) 禁用词
    banned = rules.get("banned_words", [])
    for shot in script.shots:
        for word in banned:
            if word and word in shot.narration:
                violations.append(Violation(
                    code="BANNED_WORD",
                    message=f"镜 {shot.shot_id} 使用品牌禁用词“{word}”。",
                    locator=f"shot:{shot.shot_id}",
                    fix_hint="替换为不含绝对化表述的说法。",
                ))
        if any(w and w in script.title for w in banned):
            violations.append(Violation(
                code="BANNED_WORD", message="标题使用品牌禁用词。", locator="title",
                fix_hint="替换为不含绝对化表述的说法。",
            ))
            break

    # 4) 免责声明
    required = rules.get("required_disclaimer")
    if required and (not script.disclaimer or required not in script.disclaimer):
        violations.append(Violation(
            code="MISSING_DISCLAIMER",
            message="缺少品牌要求的免责声明。",
            locator="disclaimer",
            fix_hint=f"在 disclaimer 字段写入：{required}",
        ))

    # 5) 事实合规
    triggers = rules.get("claim_triggers", [])
    for shot in script.shots:
        refs = list(shot.fact_refs or [])
        cited.extend(refs)

        invalid = [r for r in refs if r not in known]
        if invalid:
            violations.append(Violation(
                code="INVALID_FACT_REF",
                message=f"镜 {shot.shot_id} 引用了不存在的素材 ID：{invalid}。",
                locator=f"shot:{shot.shot_id}",
                fix_hint="只能引用 search_materials 实际返回过的 material_id。",
            ))

        valid_refs = [r for r in refs if r in known]
        ref_text = "".join(known[r]["snippet"] for r in valid_refs)

        hit_trigger = [t for t in triggers if t in shot.narration]
        if hit_trigger and not valid_refs:
            violations.append(Violation(
                code="UNSUPPORTED_CLAIM",
                message=(
                    f"镜 {shot.shot_id} 含产品断言（{hit_trigger[0]}）但未绑定素材 ID，属于无依据陈述。"
                ),
                locator=f"shot:{shot.shot_id}",
                fix_hint="为该镜头补充 fact_refs，或改写为不含产品事实的表达。",
            ))

        # 数字类断言：素材原文必须包含同样的数字
        numbers = _NUMBER.findall(shot.narration)
        for num in numbers:
            if not re.search(rf"(?<!\d){re.escape(num)}(?!\d)", ref_text):
                code = "PERCENT_CLAIM_UNSOURCED" if _PERCENT.search(shot.narration) else "NUMBER_CLAIM_UNSOURCED"
                violations.append(Violation(
                    code=code,
                    message=(
                        f"镜 {shot.shot_id} 出现数字“{num}”，但所引素材原文中不存在该数据"
                        f"（引用：{valid_refs or '无'}）。"
                    ),
                    locator=f"shot:{shot.shot_id}",
                    fix_hint="删除该数据，或改为素材原文中的表述。",
                ))
                break

        if valid_refs and score(shot.narration, ref_text) <= 0:
            violations.append(Violation(
                code="FACT_REF_MISMATCH",
                severity="warn",
                message=f"镜 {shot.shot_id} 的引用与该镜纳稿内容关联度低，请人工复核。",
                locator=f"shot:{shot.shot_id}",
                fix_hint="确认引用素材是否真的支撑该镜内容。",
            ))

    ok = not any(v.severity == "error" for v in violations)
    return CheckReport(
        ok=ok,
        violations=violations,
        checked_shots=len(script.shots),
        cited_materials=sorted(set(cited)),
    )


@register(
    name="check_rules",
    description=(
        "按品牌规则与平台规则检查当前稿件：时长、分镜数、标题字数、禁用词、免责声明、"
        "以及“产品断言/数据是否有素材依据”。必须先 estimate_duration 再调用。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def check_rules_handler(state: dict, args: dict) -> ToolResult:
    raw = state.get("script")
    if not raw:
        return ToolResult(
            ok=False, tool="check_rules",
            summary="当前还没有稿件，请先 submit_script。", error="no_script",
        )
    duration_raw = state.get("duration")
    duration = DurationReport(**duration_raw) if duration_raw else None
    rules = config.load_brand_rules()
    report = check_script(Script(**raw), state.get("materials") or [], rules, duration)

    if report.ok:
        warns = [v for v in report.violations if v.severity == "warn"]
        summary = "规则检查通过，未发现阻断性问题。"
        if warns:
            summary += " 另有 " + str(len(warns)) + " 条提示：" + "; ".join(v.message for v in warns)
        summary += " 可以调用 finalize_script 提交终稿。"
    else:
        lines = [
            f"[{v.severity}/{v.code}] {v.message} 修复建议：{v.fix_hint or '按提示调整'}"
            for v in report.violations
        ]
        summary = "规则检查未通过，共 %d 项问题：\n%s\n请据此修改稿件后重新 submit_script。" % (
            len([v for v in report.violations if v.severity == "error"]), "\n".join(lines)
        )

    return ToolResult(
        ok=True, tool="check_rules", summary=summary,
        data=report.model_dump(), state_patch={"check": report.model_dump(), "phase": "verify"},
    )
