"""上限与异常收敛测试：任何路径都必须以明确的 status 结束，且不抛异常。"""

from __future__ import annotations

import pytest

from shortvideo_agent import config
from shortvideo_agent.agent import run_case
from shortvideo_agent.llm import MockLLMClient
from shortvideo_agent.llm.base import LLMClient, LLMError, ToolInvocation

BRIEF = {
    "topic": "露营投影仪怎么选",
    "platform": "douyin",
    "target_seconds": 30,
    "product_ids": ["P02"],
}


class EndlessSearchClient(LLMClient):
    """永远检索同一个查不到的词，用来验证步数/调用上限。"""

    mode = "mock"

    def decide(self, state, available_tools):
        return ToolInvocation(
            "search_materials",
            {"query": "zzz-not-exist", "reason": "上限测试：持续检索不会终止的查询"},
        )


class SilentClient(LLMClient):
    """从不返回工具调用，验证协议层的兜底。"""

    mode = "mock"

    def decide(self, state, available_tools):
        return None


class BrokenClient(LLMClient):
    """模型服务不可用。"""

    mode = "llm"

    def decide(self, state, available_tools):
        raise LLMError("connection reset")


def make_case(**overrides):
    return {"case_id": "limit_case", "brief": {**BRIEF, **overrides}}


def test_step_limit_returns_incomplete(monkeypatch):
    monkeypatch.setattr(config, "MAX_STEPS", 3)
    result, _, state = run_case(make_case(), EndlessSearchClient())
    assert result.status.value == "incomplete_limit_reached"
    assert result.steps <= config.MAX_STEPS + 1
    assert any("未完成" in m for m in result.messages)


def test_tool_call_limit_returns_incomplete(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS", 2)
    result, _, _ = run_case(make_case(), EndlessSearchClient())
    assert result.status.value == "incomplete_limit_reached"
    assert result.tool_calls <= config.MAX_TOOL_CALLS + 1


def test_model_without_tool_call_is_terminated():
    result, trace, _ = run_case(make_case(), SilentClient())
    assert result.status.value == "incomplete_limit_reached"
    assert any(e.get("tool") is None for e in trace)


def test_default_limits_stop_before_recursion_limit():
    """默认配置下，必须由业务上限先停住，而不是被 LangGraph 递归上限截断。"""
    result, _, _ = run_case(make_case(), EndlessSearchClient())
    assert result.status.value == "incomplete_limit_reached"
    assert not any("图递归" in m for m in result.messages)
    assert result.tool_calls <= config.MAX_TOOL_CALLS + 1


def test_model_unavailable_returns_tool_error():
    result, _, _ = run_case(make_case(), BrokenClient())
    assert result.status.value == "failed_tool_error"
    assert any("模型不可用" in m for m in result.messages)


def test_repair_limit_leads_to_explicit_failure():
    # 目标 5 秒：受最少分镜数与每镜留白限制，5 镜不可能压到 6 秒内
    result, trace, state = run_case(make_case(target_seconds=5), MockLLMClient())
    assert result.status.value == "failed_unfixable"
    assert state["repair_rounds"] >= config.MAX_REPAIR_ROUNDS
    assert any("无法满足" in m for m in result.messages)
