"""五条验收路径 + 题目参考实例，全部用离线 Mock 跑 LangGraph。

断言的是“行为”，不是“文案”：
- 状态是否落在正确枚举；
- 时长是否由工具核算并通过复检；
- 素材引用是否可追溯；
- trace 是否记录了 决策 → 工具参数 → 返回摘要 → 反应依据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortvideo_agent.agent import run_case
from shortvideo_agent.llm import MockLLMClient

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "examples" / "inputs"


def run(name: str):
    case = json.loads((INPUTS / f"{name}.json").read_text(encoding="utf-8"))
    return run_case(case, MockLLMClient())


def tool_entries(trace: list[dict]) -> list[dict]:
    return [e for e in trace if e.get("actor") == "tool"]


def model_entries(trace: list[dict]) -> list[dict]:
    return [e for e in trace if e.get("actor") == "model"]


# ---------------------------------------------------------------- 正常生成
def test_normal_generation_completes():
    result, trace, _ = run("01_normal")
    assert result.status.value == "completed"
    assert result.script is not None
    assert len(result.script.shots) >= 3
    assert result.duration is not None and result.duration.within_tolerance
    assert result.estimated_total_seconds == result.duration.total_seconds
    assert result.messages


def test_normal_generation_every_fact_is_traceable():
    result, _, state = run("01_normal")
    known = {m["material_id"] for m in state["materials"]}
    for shot in result.script.shots:
        for ref in shot.fact_refs:
            assert ref in known, f"{ref} 不在已检索素材中"
    assert result.fact_map, "应当有可追溯的素材来源"
    assert result.check is not None and result.check.ok


# ---------------------------------------------------------------- 缺关键信息
def test_missing_info_asks_instead_of_guessing():
    result, trace, state = run("02_missing_info")
    assert result.status.value == "needs_input"
    assert result.script is None
    assert result.tool_calls == 1
    assert tool_entries(trace)[0]["tool"] == "request_clarification"
    joined = " ".join(result.messages)
    assert "发布平台" in joined and "目标时长" in joined


# ---------------------------------------------------------------- 无素材
def test_no_material_rewrites_query_then_requests_input():
    result, trace, state = run("03_no_material")
    assert result.status.value == "needs_input"
    assert result.script is None

    queries = [e["args"]["query"] for e in model_entries(trace) if e.get("tool") == "search_materials"]
    assert len(queries) >= 2, "检索无结果时应改写查询"
    assert len(set(queries)) == len(queries), "每次改写都应产生不同查询"
    assert state["search_attempts"] == 3

    last = tool_entries(trace)[-1]
    assert last["tool"] == "request_clarification"


# ---------------------------------------------------------------- 工具失败
def test_tool_failure_is_observed_then_recovered():
    result, trace, _ = run("04_tool_failure")
    assert result.status.value == "completed"

    failed = [e for e in tool_entries(trace) if not e["ok"]]
    assert failed and failed[0]["tool"] == "search_materials"
    assert failed[0]["error"] == "injected_tool_failure"

    # 失败后模型换了查询角度并成功
    searches = [e for e in model_entries(trace) if e.get("tool") == "search_materials"]
    assert len(searches) >= 2
    assert searches[0]["args"]["query"] != searches[1]["args"]["query"]


# ---------------------------------------------------------------- 超时长修正
def test_over_duration_is_compressed_and_rechecked():
    result, trace, state = run("05_over_duration")

    estimates = [e for e in tool_entries(trace) if e["tool"] == "estimate_duration"]
    assert len(estimates) >= 2, "必须先用工具估算、修正后再估算"

    # 第二次提交稿件这一动作，是在“超时长”观察之后做出的
    repairs = [e for e in model_entries(trace) if e.get("tool") == "submit_script"]
    assert len(repairs) >= 2, "应当提交初稿并提交修正稿"
    assert "超出目标" in repairs[1]["reacts_to"], "模型应当基于超时长的观察调整动作"

    assert result.status.value == "completed"
    assert result.estimated_total_seconds is not None
    assert result.estimated_total_seconds <= 31
    assert state["repair_rounds"] >= 1

    # 修正后的稿件必须重新通过规则检查
    assert result.check is not None and result.check.ok


# ---------------------------------------------------------------- 参考实例（P01）
def test_reference_case_p01_only_subtitle_fact():
    result, trace, state = run("06_reference_p01")
    assert result.status.value == "completed"
    assert set(result.fact_map) == {"P01-F1"}
    assert result.fact_map["P01-F1"] == ["支持字幕"]
    assert result.estimated_total_seconds <= 31

    estimates = [e for e in tool_entries(trace) if e["tool"] == "estimate_duration"]
    first_over = json.loads(json.dumps(estimates[0]["result_summary"]))
    assert "超出目标" in first_over

    # 除 P01-F1 外，任何镜头都不得携带产品事实引用
    for shot in result.script.shots:
        for ref in shot.fact_refs:
            assert ref == "P01-F1"


# ---------------------------------------------------------------- 改变时长 → 动作调整
def test_tight_duration_forces_repair():
    result, trace, state = run("07_tight_duration")

    estimates = [e for e in tool_entries(trace) if e["tool"] == "estimate_duration"]
    assert len(estimates) >= 2, "短时长下必须估算→改稿→再估算"
    assert state["repair_rounds"] >= 1

    assert result.status.value == "completed"
    assert result.estimated_total_seconds <= 19


def test_shorter_target_changes_next_action():
    """同一主题、同一素材，只改目标时长，模型的动作序列必须不同。"""
    _, trace_30, _ = run("01_normal")
    _, trace_18, _ = run("07_tight_duration")

    seq_30 = [e["tool"] for e in tool_entries(trace_30)]
    seq_18 = [e["tool"] for e in tool_entries(trace_18)]

    assert seq_30.count("estimate_duration") == 1
    assert seq_18.count("estimate_duration") == 2
    assert seq_18.count("submit_script") == 2
    assert len(seq_30) < len(seq_18)

    # 18 秒那次必须观察到“超出目标”，30 秒那次不应出现
    over_18 = [e for e in tool_entries(trace_18) if "超出目标" in (e.get("result_summary") or "")]
    over_30 = [e for e in tool_entries(trace_30) if "超出目标" in (e.get("result_summary") or "")]
    assert over_18 and not over_30


# ---------------------------------------------------------------- trace 完整性
@pytest.mark.parametrize("name", ["01_normal", "03_no_material", "04_tool_failure", "05_over_duration"])
def test_trace_records_full_loop(name):
    _, trace, _ = run(name)
    models = model_entries(trace)
    assert models, "至少要有一次模型动作"
    for entry in models:
        assert entry.get("decision_basis"), "每次动作都要有决策依据"
        assert entry.get("phase")
    # 交替出现：模型决策后紧跟该动作的工具执行
    for i, entry in enumerate(trace[:-1]):
        if entry.get("actor") == "model" and entry.get("tool"):
            nxt = trace[i + 1]
            assert nxt.get("actor") == "tool"
            assert nxt.get("result_summary")
