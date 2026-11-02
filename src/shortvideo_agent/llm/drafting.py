"""脚本草稿的构造与压缩。

**这是离线 Mock 的“写作能力”替身**，用确定性算法模拟模型的两个行为：
1. 初稿按目标时长的 1.27 倍撰写（模拟真实模型初稿普遍偏长）；
2. 收到“超时长”观察后，按预算削减旁白字数并保留事实句。

真实模型模式下这些函数不会被调用，脚本由模型直接产出。
"""

from __future__ import annotations

import math
import re

from .. import config
from ..models import MaterialHit, Script, Shot
from ..tools.duration import spoken_chars

_SENT_SPLIT = re.compile(r"(?<=[。！？])")

# 与品类无关、不含产品断言、不含数字的填充句（按字数降序，便于贪心补齐）
_GENERIC_LINES = [
    "先看自己的使用场景再选型号。",
    "日常通勤或者出差都能带上。",
    "整体比预期顺手一些。",
    "细节用起来才知道。",
    "不用特意腾出时间。",
    "这类需求一直都有。",
    "有类似需求可以留意。",
    "朋友一起用更合适。",
    "上手门槛并不高。",
    "维护起来也省心。",
    "这点很关键。",
    "够用。",
]

# 补字时允许的缺口：宁可少两个字，也不要用“很好。很好。”这种堆叠把脚本写烂
_PAD_SLACK = 2


def chars_budget(target_seconds: int, shots: int, cps: float, ratio: float) -> int:
    """按“扣除每镜固定留白后的可用秒数 × 语速”换算允许的旁白字数。"""
    usable = target_seconds * ratio - shots * config.SHOT_PADDING_SECONDS
    return max(0, int(usable * cps))


def _pad_to(text: str, target: int, pool: list[str], used: set[str] | None = None) -> str:
    used = used if used is not None else set()
    cur = spoken_chars(text)
    for cand in pool:
        if cur >= target - _PAD_SLACK:
            break
        if cand in used:
            continue
        n = spoken_chars(cand)
        if cur + n <= target:
            text += cand
            cur += n
            used.add(cand)
    return text


def _title(topic: str, limit: int) -> str:
    clean = topic.strip().replace(" ", "")
    return clean[:limit]


def compose_draft(
    brief: dict,
    materials: list[dict],
    ratio: float | None = None,
) -> Script:
    """五镜结构：钩子 → 产品引入 → 功能细节 → 体验 → 行动号召。

    ratio 用来模拟“初稿偏长/克制”两种情形，便于构造“一次通过”与“超时长修正”两条路径。
    """
    platform = brief.get("platform") or "douyin"
    target = int(brief.get("target_seconds") or 30)
    profiles = config.load_platform_profiles()
    profile = profiles.get(platform)
    if profile is None:
        raise ValueError(f"平台 `{platform}` 未配置，可用：{', '.join(sorted(profiles))}")
    cps = float(profile["chars_per_second"])
    rules = config.load_brand_rules()
    topic = brief.get("topic") or "产品介绍"

    shots_n = 5
    total_budget = chars_budget(target, shots_n, cps, ratio or config.DRAFT_OVERWRITE_RATIO)
    per = max(8, total_budget // shots_n)

    # 按素材编号取前三条，保证“先讲主参数、再讲细节”的叙述顺序稳定可复现
    hits = sorted((MaterialHit(**m) for m in materials), key=lambda h: h.material_id)
    refs = hits[:3]

    def seg(idx: int) -> str:
        if idx == 0:
            return f"你有没有遇到过{topic}的纠结时刻？"
        if idx == shots_n - 1:
            return "想看更多这类内容，评论区聊聊你的使用场景。"
        if idx - 1 < len(refs):
            hit = refs[idx - 1]
            clean_name = re.sub(rf"^{re.escape(hit.product_id)}\s*", "", hit.product_name).strip()
            prefix = f"{clean_name} " if idx == 1 else "另外，它"
            return f"{prefix}{hit.snippet}。"
        return ""

    shots: list[Shot] = []
    for idx in range(shots_n):
        ref = []
        if 1 <= idx <= len(refs) and idx - 1 < len(refs):
            ref = [refs[idx - 1].material_id]
        pool = _GENERIC_LINES[idx:] + _GENERIC_LINES[:idx]
        narration = _pad_to(seg(idx), per, pool)
        shots.append(Shot(
            shot_id=idx + 1,
            visual=_visuals(idx),
            narration=narration,
            fact_refs=ref,
        ))

    return Script(
        title=_title(topic, int(rules.get("title_max_chars", 20))),
        platform=platform,
        target_seconds=target,
        shots=shots,
        disclaimer=rules.get("required_disclaimer"),
    )


def _visuals(idx: int) -> str:
    pool = [
        "开场：用户面对具体使用场景，镜头推近细节",
        "产品出场特写，主体居中，光线干净",
        "功能演示：手部操作与画面同步",
        "使用中的反应镜头，环境氛围光",
        "收尾：产品与场景同框，字幕落版",
    ]
    return pool[idx % len(pool)]


def trim_to_spoken(text: str, budget: int) -> str:
    """按句子从后往前删，保留前面的句子（事实句写在最前面）。"""
    if spoken_chars(text) <= budget:
        return text
    sentences = [s for s in _SENT_SPLIT.split(text) if s]
    while len(sentences) > 1 and spoken_chars("".join(sentences)) > budget:
        sentences.pop()
    text = "".join(sentences)
    if spoken_chars(text) > budget:
        # 单句仍然超预算：按字符硬截断，保留句号
        limit = max(1, budget)
        text = text[:limit].rstrip("，、；。") + "。"
    return text


def compress_script(script: Script, platform: str | None = None) -> Script:
    """依据目标时长重算预算并压缩，返回新稿（不改动入参）。"""
    platform = platform or script.platform
    profiles = config.load_platform_profiles()
    profile = profiles.get(platform)
    if profile is None:
        raise ValueError(f"平台 `{platform}` 未配置，可用：{', '.join(sorted(profiles))}")
    cps = float(profile["chars_per_second"])
    n = len(script.shots)
    budget = chars_budget(script.target_seconds, n, cps, config.REPAIR_BUDGET_RATIO)
    total = sum(spoken_chars(s.narration) for s in script.shots)
    if total <= budget or total == 0:
        return script.model_copy(deep=True)

    ratio = budget / total
    new_shots: list[Shot] = []
    for i, shot in enumerate(script.shots):
        quota = max(6, int(math.floor(spoken_chars(shot.narration) * ratio)))
        sentences = [s for s in _SENT_SPLIT.split(shot.narration) if s]
        # 保留首句：事实句按约定写在每一镜开头，压缩时不能删
        head = sentences[0] if (shot.fact_refs and sentences) else ""
        quota = max(quota, spoken_chars(head))  # 事实句不可截断
        # 先删尾句保住原稿措辞，不足再按轮转池补齐，避免各镜旁白雷同
        text = trim_to_spoken(shot.narration, quota)
        pool = _GENERIC_LINES[i:] + _GENERIC_LINES[:i]
        text = _pad_to(text, quota, pool)
        new_shots.append(shot.model_copy(update={"narration": text}))
    return script.model_copy(update={"shots": new_shots})
