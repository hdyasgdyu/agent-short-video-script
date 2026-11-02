from .base import LLMClient, LLMError, ToolInvocation  # noqa: F401
from .mock_client import MockLLMClient  # noqa: F401
from .openai_client import OpenAICompatClient  # noqa: F401


def make_client(mode: str = "mock"):
    """工厂：mock 走离线策略表，llm 走 OpenAI 兼容端点。"""
    if mode == "mock":
        return MockLLMClient()
    if mode == "llm":
        return OpenAICompatClient()
    raise ValueError(f"未知模式：{mode}（可选 mock / llm）")
