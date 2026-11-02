"""OpenAI 兼容客户端（DeepSeek / 通义千问 / GLM / vLLM / Ollama 兼容层均可）。

只用 httpx 直连 /chat/completions，不引入厂商 SDK：
模型可替换是本项目的显式取舍，绑定单一 SDK 会让评审者无法用自己手上的 key 验证。
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from ..agent.prompts import build_system_prompt, build_user_prompt
from ..tools import tool_schemas
from .base import LLMClient, LLMError, ToolInvocation


class OpenAICompatClient(LLMClient):
    mode = "llm"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("MODEL_NAME") or "gpt-4o-mini"
        self.timeout = timeout
        if not self.base_url or not self.api_key:
            raise LLMError(
                "真实模型模式需要 OPENAI_BASE_URL 与 OPENAI_API_KEY，"
                "可复制 .env.example 为 .env 后填写；无 key 时请使用 --mode mock。"
            )

    def describe(self) -> str:
        host = urlparse(self.base_url).netloc or self.base_url
        return f"{host} / {self.model}"

    def short_name(self) -> str:
        return self.model

    def decide(self, state: dict[str, Any], available_tools: list[str]) -> ToolInvocation | None:
        schemas = [
            s for s in tool_schemas(state.get("phase", "research"))
            if s["function"]["name"] in available_tools
        ]
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": build_system_prompt(state)},
                {"role": "user", "content": build_user_prompt(state)},
            ],
            "tools": schemas,
            "tool_choice": "auto",
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"模型请求失败：{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(f"模型返回 {resp.status_code}: {resp.text[:300]}")

        try:
            message = resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LLMError(f"模型响应结构异常：{resp.text[:300]}") from exc

        calls = message.get("tool_calls") or []
        if not calls:
            return None
        fn = calls[0].get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError:
            return ToolInvocation(name or "invalid_json", {"reason": f"参数不是合法 JSON：{raw_args[:120]}"})
        if not isinstance(args, dict):
            return None
        return ToolInvocation(name, args)
