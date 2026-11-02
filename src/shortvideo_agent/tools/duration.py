"""时长估算工具。

口径：每秒中文字数（cps）由平台配置决定，单镜时长 = 去标点字数 / cps + 固定留白。
这是确定性计算，**不接受模型自报时长**——最终交付的 total 只能来自本工具。
"""

from __future__ import annotations

import math
import re

from .. import config
from ..models import DurationReport, Shot, ToolResult
from .registry import register

_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()《》\-—…·]+")


def spoken_chars(text: str) -> int:
    return len(_PUNCT.sub("", text or ""))


def estimate(shots: list[Shot], platform: str, target_seconds: int) -> DurationReport:
    profiles = config.load_platform_profiles()
    profile = profiles.get(platform)
    if profile is None:
        # 不做静默降级：用错语速会让“时长达标”变成假象
        raise ValueError(
            f"平台 `{platform}` 未配置，无法确定语速。"
            f"可用平台：{', '.join(sorted(profiles))}。"
            "新增平台只需在 data/platform_profiles.json 增加一项，代码无需改动。"
        )
    cps = float(profile["chars_per_second"])

    per_shot: dict[str, float] = {}
    total = 0.0
    for shot in shots:
        chars = spoken_chars(shot.narration)
        seconds = math.ceil((chars / cps) * 10) / 10 + config.SHOT_PADDING_SECONDS
        seconds = round(seconds, 1)
        per_shot[str(shot.shot_id)] = seconds
        total += seconds

    total = round(total, 1)
    over_by = round(max(0.0, total - target_seconds), 1)
    return DurationReport(
        cps=cps,
        per_shot=per_shot,
        total_seconds=total,
        target_seconds=target_seconds,
        over_by=over_by,
        within_tolerance=total <= target_seconds + config.DURATION_TOLERANCE_SECONDS,
    )


@register(
    name="estimate_duration",
    description=(
        "对当前稿件按平台语速估算总时长与每镜时长。"
        "只要修改过稿件就必须重新调用，最终交付的总时长以此工具返回为准。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def estimate_duration_handler(state: dict, args: dict) -> ToolResult:
    raw = state.get("script")
    if not raw:
        return ToolResult(
            ok=False, tool="estimate_duration",
            summary="当前还没有稿件，无法估算时长。请先 submit_script。",
            error="no_script",
        )

    script_shots = [Shot(**s) for s in raw["shots"]]
    report = estimate(script_shots, raw["platform"], int(raw["target_seconds"]))
    patch = {"duration": report.model_dump(), "phase": "verify"}

    if report.within_tolerance:
        summary = (
            f"估算总时长 {report.total_seconds}s / 目标 {report.target_seconds}s，"
            f"在容差内（每镜：{report.per_shot}）。下一步可调用 check_rules 做规则检查。"
        )
    else:
        summary = (
            f"估算总时长 {report.total_seconds}s，超出目标 {report.target_seconds}s 共 {report.over_by}s。"
            f"需要压缩旁白字数或删减镜头后重新 submit_script，再重新估算。"
            f"每镜时长：{report.per_shot}；语速 {report.cps} 字/秒。"
        )

    return ToolResult(
        ok=True, tool="estimate_duration", summary=summary,
        data=report.model_dump(), state_patch=patch,
    )
