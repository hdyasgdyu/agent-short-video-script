"""防编造校验：这是本题的关键红线，单独成文件。"""

from __future__ import annotations

from shortvideo_agent import config
from shortvideo_agent.models import Script, Shot
from shortvideo_agent.tools.duration import estimate
from shortvideo_agent.tools.rules import check_script
from shortvideo_agent.tools.search_materials import load_corpus

RULES = config.load_brand_rules()
CORPUS = load_corpus()


def run_check(narrations: list[tuple[str, list[str]]]) -> set[str]:
    shots = [
        Shot(shot_id=i + 1, visual="画面", narration=text, fact_refs=refs)
        for i, (text, refs) in enumerate(narrations)
    ]
    script = Script(
        title="测试标题",
        platform="douyin",
        target_seconds=30,
        shots=shots,
        disclaimer=RULES["required_disclaimer"],
    )
    duration = estimate(script.shots, "douyin", 30)
    report = check_script(script, CORPUS, RULES, duration)
    return {v.code for v in report.violations}


def test_percent_effect_data_without_source_is_blocked():
    codes = run_check([
        ("这款产品效率提升 80%。", []),
        ("再补充一点使用感受。", []),
        ("再看一个细节。", []),
    ])
    assert "PERCENT_CLAIM_UNSOURCED" in codes


def test_product_claim_without_ref_is_blocked():
    codes = run_check([
        ("这款会议耳机支持实时字幕。", []),
        ("日常开会挺方便的。", []),
        ("再看一个细节。", []),
    ])
    assert "UNSUPPORTED_CLAIM" in codes


def test_number_not_in_source_is_blocked():
    # P02-F2 原文是“续航 2.5 小时”，写 8 小时属于篡改数据
    codes = run_check([
        ("便携投影仪 S2 续航 8 小时。", ["P02-F2"]),
        ("日常通勤或者出差都能带上。", []),
        ("这点很关键。", []),
    ])
    assert "NUMBER_CLAIM_UNSOURCED" in codes


def test_grounded_number_passes():
    codes = run_check([
        ("便携投影仪 S2 支持 1080P 物理分辨率。", ["P02-F1"]),
        ("日常通勤或者出差都能带上。", []),
        ("这点很关键。", []),
    ])
    assert "NUMBER_CLAIM_UNSOURCED" not in codes
    assert "UNSUPPORTED_CLAIM" not in codes


def test_banned_word_is_blocked():
    codes = run_check([
        ("这是市面上最好的会议耳机。", []),
        ("日常开会挺方便的。", []),
        ("再看一个细节。", []),
    ])
    assert "BANNED_WORD" in codes


def test_invalid_material_ref_is_blocked():
    codes = run_check([
        ("智能会议耳机 X1 支持字幕。", ["P01-F9"]),
        ("日常开会挺方便的。", []),
        ("再看一个细节。", []),
    ])
    assert "INVALID_FACT_REF" in codes
