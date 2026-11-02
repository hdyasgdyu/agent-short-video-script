"""离线 Mock 模型：用确定性策略表模拟“观察 → 调整动作”。

**边界声明（同时写进 README 与 docs/03）**：
Mock 只证明流程可跑通、工具契约可用、trace 可复现，
不证明真实模型的决策质量。策略表是人工写死的，真实模型则会自由选择动作。
"""

from __future__ import annotations

from typing import Any

from .. import config
from ..models import Script
from .base import LLMClient, ToolInvocation
from .drafting import compress_script, compose_draft

_QUERY_REWRITES = (
    "{topic}",
    "{topic} 参数 规格",
    "{core} 说明",
)


def _core(topic: str) -> str:
    return topic.strip()[:6] or topic


class MockLLMClient(LLMClient):
    mode = "mock"

    def describe(self) -> str:
        return "mock / 离线策略表（非真实模型决策）"

    def short_name(self) -> str:
        return "mock"

    def decide(self, state: dict[str, Any], available_tools: list[str]) -> ToolInvocation | None:
        phase = state.get("phase", "research")
        brief = state.get("brief") or {}
        observations = state.get("observations") or []
        last = observations[-1] if observations else None
        missing = state.get("missing_fields") or []

        def can(name: str) -> bool:
            return name in available_tools

        # —— 关键信息缺失：直接请求补充，不做任何检索
        if missing and can("request_clarification"):
            return ToolInvocation("request_clarification", {
                "questions": [f"请提供{_LABELS.get(f, f)}：{_hint(f)}" for f in missing],
                "reason": f"缺少必填字段 {missing}，继续生成会变成猜测，先请用户补充。",
            })

        if phase == "research":
            return self._research(state, brief, last, available_tools)

        if phase == "draft":
            script = compose_draft(
                brief,
                state.get("materials") or [],
                ratio=state.get("mock_draft_ratio"),
            )
            return ToolInvocation("submit_script", {
                "script": script.model_dump(),
                "reason": (
                    f"已拿到 {len(state.get('materials') or [])} 条可引用素材，"
                    "按钩子/引入/细节/体验/号召五镜结构成稿。"
                ),
            })

        if phase == "verify":
            return self._verify(state, available_tools, last)

        return None

    # ------------------------------------------------------------ research
    def _research(self, state, brief, last, available) -> ToolInvocation | None:
        topic = brief.get("topic") or ""
        observations = state.get("observations") or []
        search_calls = sum(1 for o in observations if o.get("tool") == "search_materials")
        last_failed = bool(last and not last.get("ok") and last.get("tool") == "search_materials")

        if search_calls >= config.MAX_SEARCH_ATTEMPTS:
            return ToolInvocation("request_clarification", {
                "questions": [
                    f"素材库中没有检索到与“{topic}”相关的产品资料，请上传产品资料或提供可引用的参数。",
                    "或者确认是否可以改为不涉及具体产品参数的通用内容。",
                ],
                "reason": (
                    f"已改写查询 {search_calls} 次仍无命中"
                    f"（{state.get('search_queries')}），没有素材就无法产出有事实依据的脚本。"
                ),
            })

        attempt = min(search_calls, len(_QUERY_REWRITES) - 1)
        query = _QUERY_REWRITES[attempt].format(topic=topic, core=_core(topic))
        if last_failed:
            query = _QUERY_REWRITES[min(search_calls, len(_QUERY_REWRITES) - 1)].format(
                topic=topic, core=_core(topic)
            )
            reason = "上一次检索工具报错，换一个查询角度重试，同时验证是工具问题还是查询问题。"
        elif attempt == 0:
            reason = "先用主题原词检索，确认素材库是否有可用依据。"
        else:
            reason = f"前一次检索 0 命中，将查询从口语化主题改写为参数/说明类关键词（第 {attempt + 1} 次）。"

        return ToolInvocation("search_materials", {"query": query, "top_k": 6, "reason": reason})

    # ------------------------------------------------------------ verify
    def _verify(self, state, available, last=None) -> ToolInvocation | None:
        duration = state.get("duration")
        check = state.get("check")
        script_raw = state.get("script")
        if not script_raw:
            return None
        script = Script(**script_raw)

        # 工具已经明确拒绝继续改稿：转为明确失败，而不是继续硬试
        if last and not last.get("ok") and last.get("error") == "repair_limit_reached":
            current = (state.get("duration") or {}).get("total_seconds")
            return ToolInvocation("report_unfixable", {
                "reason": f"已用完 {config.MAX_REPAIR_ROUNDS} 轮改稿机会，时长仍无法压进目标区间。",
                "best_effort": f"当前最接近方案总时长 {current}s，目标 {script.target_seconds}s。",
            })

        if not duration:
            return ToolInvocation("estimate_duration", {
                "reason": "稿件已提交，先按平台语速核算实际时长，再决定是否需要压缩。",
            })

        if not duration.get("within_tolerance"):
            if "submit_script" in available:
                fixed = compress_script(script)
                return ToolInvocation("submit_script", {
                    "script": fixed.model_dump(),
                    "reason": (
                        f"估算 {duration['total_seconds']}s，超出目标 {duration['over_by']}s，"
                        "按预算压缩旁白字数并保留事实句，提交后重新估算。"
                    ),
                })
            return ToolInvocation("report_unfixable", {
                "reason": "时长超标但已无修改机会。",
            })

        if not check:
            return ToolInvocation("check_rules", {
                "reason": "时长在容差内，继续检查品牌规则与事实引用是否合规。",
            })

        if check.get("ok"):
            return ToolInvocation("finalize_script", {
                "reason": "时长与规则检查均通过，提交终稿。",
            })

        errors = [v for v in check.get("violations", []) if v.get("severity") == "error"]
        fixed = self._apply_fixes(script, errors)
        if fixed is None or "submit_script" not in available:
            return ToolInvocation("report_unfixable", {
                "reason": "规则检查存在无法自动修正的问题：" + "; ".join(v["message"] for v in errors),
                "best_effort": f"当前稿件估算 {duration['total_seconds']}s。",
            })
        return ToolInvocation("submit_script", {
            "script": fixed.model_dump(),
            "reason": "规则检查有 error，按违规项逐条修正后重新提交并复检：" + "; ".join(
                v["code"] for v in errors
            ),
        })

    def _apply_fixes(self, script: Script, errors: list[dict]) -> Script | None:
        rules = config.load_brand_rules()
        data = script.model_dump()
        fixable = {
            "MISSING_DISCLAIMER", "TITLE_TOO_LONG", "TOO_FEW_SHOTS", "BANNED_WORD",
            "DURATION_OVERRUN",
        }
        if any(v["code"] not in fixable for v in errors):
            return None
        if any(v["code"] == "MISSING_DISCLAIMER" for v in errors):
            data["disclaimer"] = rules.get("required_disclaimer")
        if any(v["code"] == "TITLE_TOO_LONG" for v in errors):
            data["title"] = data["title"][: int(rules.get("title_max_chars", 20))]
        if any(v["code"] == "TOO_FEW_SHOTS" for v in errors):
            while len(data["shots"]) < int(rules.get("min_shots", 3)):
                idx = len(data["shots"]) + 1
                data["shots"].append({
                    "shot_id": idx,
                    "visual": "补充镜头：产品细节特写",
                    "narration": "细节再看一眼，信息更完整。",
                    "duration_est": 0,
                    "fact_refs": [],
                })
        return Script(**data)


_LABELS = {
    "topic": "主题",
    "platform": "发布平台",
    "target_seconds": "目标时长",
    "audience": "目标受众",
    "style": "内容风格",
}


def _hint(field: str) -> str:
    platforms = " / ".join(sorted(config.load_platform_profiles()))
    return {
        "topic": "这次视频的主题，例如“露营投影仪怎么选”",
        "platform": f"发布平台，当前可用 {platforms}",
        "target_seconds": "目标时长（秒），例如 30",
        "audience": "目标受众",
        "style": "内容风格",
    }.get(field, field)
