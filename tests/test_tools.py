"""工具层单测：时长估算、素材检索、规则检查、稿件提交守卫。"""

from __future__ import annotations

import pytest

from shortvideo_agent import config
from shortvideo_agent.models import Script, Shot
from shortvideo_agent.tools.duration import estimate, spoken_chars
from shortvideo_agent.tools.registry import execute_tool
from shortvideo_agent.tools.rules import check_script
from shortvideo_agent.tools.search_materials import load_corpus, search

RULES = config.load_brand_rules()


def make_script(narration: str = "智能会议耳机 X1 支持字幕。", refs: list[str] | None = None) -> Script:
    shots = [
        Shot(shot_id=i + 1, visual="画面", narration=narration, fact_refs=list(refs or []))
        for i in range(5)
    ]
    return Script(
        title="测试标题",
        platform="douyin",
        target_seconds=30,
        shots=shots,
        disclaimer=RULES["required_disclaimer"],
    )


# ---------------------------------------------------------------- 时长估算
def test_spoken_chars_ignores_punctuation_and_spaces():
    assert spoken_chars("你好，世界！") == 4
    assert spoken_chars(" 支持 1080P ") == 7


def test_estimate_is_deterministic_and_matches_formula():
    shots = [Shot(shot_id=i + 1, narration="字" * 40) for i in range(5)]
    report = estimate(shots, "douyin", 30)
    # 40 字 / 5.5 字每秒 = 7.2727 -> 向上取到 0.1 位 7.3；再加 0.3 秒留白 = 7.6
    assert report.per_shot["1"] == pytest.approx(7.6)
    assert report.total_seconds == pytest.approx(38.0)
    assert report.over_by == pytest.approx(8.0)
    assert report.within_tolerance is False


def test_estimate_rejects_unknown_platform():
    """未知平台不能静默套用抖音语速，否则“时长达标”是假的。"""
    shots = [Shot(shot_id=1, narration="字" * 20)]
    with pytest.raises(ValueError) as exc:
        estimate(shots, "tiktok", 30)
    assert "未配置" in str(exc.value)
    assert "douyin" in str(exc.value)


def test_estimate_within_tolerance():
    shots = [Shot(shot_id=i + 1, narration="字" * 29) for i in range(5)]
    report = estimate(shots, "douyin", 30)
    assert report.total_seconds == pytest.approx(28.0)
    assert report.within_tolerance is True


# ---------------------------------------------------------------- 检索
def test_search_hits_product_facts():
    hits = search("露营投影仪怎么选", product_ids=["P02"])
    assert hits, "应当命中 P02 的素材"
    assert all(h.product_id == "P02" for h in hits)
    assert all(h.material_id.startswith("P02-") for h in hits)


def test_search_returns_empty_for_unknown_topic():
    assert search("量子猫砂盆的AI健康监测") == []


def test_p01_contains_only_subtitle_fact():
    facts = [m for m in load_corpus() if m["product_id"] == "P01"]
    assert len(facts) == 1
    assert facts[0]["material_id"] == "P01-F1"
    assert facts[0]["snippet"] == "支持字幕"


# ---------------------------------------------------------------- 规则检查
def test_rules_reject_duration_overrun():
    script = make_script("字" * 40)
    duration = estimate(script.shots, "douyin", 30)
    report = check_script(script, load_corpus(), RULES, duration)
    assert report.ok is False
    assert "DURATION_OVERRUN" in {v.code for v in report.violations}


def test_rules_require_duration_check_first():
    script = make_script()
    report = check_script(script, load_corpus(), RULES, None)
    assert "MISSING_DURATION_CHECK" in {v.code for v in report.violations}


def test_rules_pass_on_grounded_script():
    script = make_script("智能会议耳机 X1 支持字幕。", ["P01-F1"])
    duration = estimate(script.shots, "douyin", 30)
    report = check_script(script, load_corpus(), RULES, duration)
    assert report.ok is True, [v.message for v in report.violations]
    assert report.cited_materials == ["P01-F1"]


# ---------------------------------------------------------------- 工具执行器
def test_unknown_tool_is_reported_not_raised():
    result = execute_tool({"phase": "research"}, "no_such_tool", {})
    assert result.ok is False
    assert result.error == "unknown_tool"


def test_tool_not_allowed_in_phase():
    result = execute_tool({"phase": "draft"}, "estimate_duration", {})
    assert result.ok is False
    assert result.error == "tool_not_allowed_in_phase"


def test_injected_fault_once_then_recovers():
    state = {"phase": "research", "faults": {"search_materials": "error_once"}, "fault_fired": {}}
    first = execute_tool(state, "search_materials", {"query": "露营投影仪"})
    assert first.ok is False
    assert first.error == "injected_tool_failure"

    state2 = {**state, "fault_fired": first.state_patch["fault_fired"]}
    second = execute_tool(state2, "search_materials", {"query": "露营投影仪"})
    assert second.ok is True


def test_submit_script_rejects_unknown_material_ref():
    script = make_script("智能会议耳机 X1 支持字幕。", ["P99-F9"]).model_dump()
    result = execute_tool({"phase": "draft", "materials": [], "brief": {}}, "submit_script", {"script": script})
    assert result.ok is False
    assert result.error == "invalid_fact_refs"


def test_finalize_requires_duration_and_check():
    script = make_script().model_dump()
    result = execute_tool({"phase": "verify", "script": script}, "finalize_script", {})
    assert result.ok is False
    assert result.error == "precheck_missing"
