"""交互模式的输入解析与会话流转测试。

不测 input() 循环本身（那是终端行为），只测可编程的两块：
自然语言 → brief 的抽取，以及一轮对话在 completed / needs_input 之间是否正确流转。
"""

from __future__ import annotations

import pytest

from shortvideo_agent.agent import run_case
from shortvideo_agent.llm import MockLLMClient, OpenAICompatClient
from shortvideo_agent.repl import Session, _ensure_client, _prompt, handle_turn, parse_brief


def test_auto_mode_falls_back_to_mock_without_config(monkeypatch, capsys):
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)

    session = Session(mode="auto")
    client = _ensure_client(session)
    assert client.mode == "mock"
    assert "已降级" in capsys.readouterr().out


# ---------------------------------------------------------------- 模型标识
def test_prompt_shows_current_model():
    session = Session(mode="mock", client=MockLLMClient())
    assert "mock" in _prompt(session)


def test_openai_client_short_name_uses_model_id(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_NAME", "deepseek-chat")

    client = OpenAICompatClient()  # 构造不发请求，只读配置
    assert client.short_name() == "deepseek-chat"
    assert "api.deepseek.com" in client.describe()
    assert "deepseek-chat" in client.describe()


# ---------------------------------------------------------------- 输入解析
def test_parse_extracts_time_platform_and_topic():
    parsed = parse_brief("帮我写个露营投影仪的 30 秒抖音脚本")
    assert parsed["target_seconds"] == 30
    assert parsed["platform"] == "douyin"
    assert parsed["topic"] == "露营投影仪"


def test_parse_extracts_minutes_and_platform_alias():
    parsed = parse_brief("做一条小红书 2 分钟的会议耳机文案")
    assert parsed["target_seconds"] == 120
    assert parsed["platform"] == "xiaohongshu"
    assert parsed["topic"] == "会议耳机"


def test_parse_extracts_product_ids():
    parsed = parse_brief("用 P01 写 30 秒抖音脚本")
    assert parsed["product_ids"] == ["P01"]
    # 只说了产品没说主题，主题由会话层用产品名兜底
    assert "topic" not in parsed


def test_turn_falls_back_to_product_as_topic(tmp_path, capsys):
    session = Session(mode="mock")
    result = handle_turn(session, "用 P01 写 30 秒抖音脚本", tmp_path)
    assert result is not None
    assert session.brief["topic"] == "P01 产品介绍"
    assert result.status.value == "completed"


def test_parse_pure_supplement_sets_no_topic():
    parsed = parse_brief("30 秒，抖音")
    assert parsed["target_seconds"] == 30
    assert parsed["platform"] == "douyin"
    assert "topic" not in parsed


# ---------------------------------------------------------------- 会话流转
def test_turn_completes_with_full_brief(tmp_path, capsys):
    session = Session(mode="mock")
    result = handle_turn(session, "帮我写个露营投影仪的 30 秒抖音脚本", tmp_path)

    assert result is not None
    assert result.status.value == "completed"
    assert result.script is not None
    assert session.last_paths["md"].exists()
    assert session.last_status == "completed"


def test_turn_asks_then_accepts_supplement(tmp_path, capsys):
    session = Session(mode="mock")

    first = handle_turn(session, "露营投影仪怎么选", tmp_path)
    assert first is not None
    assert first.status.value == "needs_input"
    assert session.last_status == "needs_input"

    second = handle_turn(session, "30 秒，抖音", tmp_path)
    assert second is not None
    assert second.status.value == "completed"
    # 补充轮不应该把原主题覆盖成“30 秒，抖音”
    assert session.brief["topic"] == "露营投影仪怎么选"


def test_turn_without_topic_prints_hint(tmp_path, capsys):
    session = Session(mode="mock")
    assert handle_turn(session, "30 秒，抖音", tmp_path) is None
    assert "先给我一个主题" in capsys.readouterr().out


# ---------------------------------------------------------------- 流式回调
def test_on_step_receives_every_trace_entry():
    case = {
        "case_id": "stream_probe",
        "brief": {
            "topic": "露营投影仪怎么选",
            "platform": "douyin",
            "target_seconds": 30,
            "product_ids": ["P02"],
        },
    }
    events: list[dict] = []
    result, trace, _ = run_case(case, MockLLMClient(), on_step=events.append)

    assert result.status.value == "completed"
    assert events, "on_step 应当被回调"
    assert len(events) == len(trace)
    assert any(e.get("actor") == "model" for e in events)
    assert any(e.get("actor") == "tool" for e in events)


@pytest.mark.parametrize("text,expected", [
    ("5分钟视频", 300),
    ("15秒 视频号", 15),
])
def test_parse_time_variants(text, expected):
    assert parse_brief(text)["target_seconds"] == expected
