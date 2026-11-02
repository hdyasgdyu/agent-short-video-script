"""模型客户端协议。

图（graph）只依赖这一个抽象：给定当前 run state 与可用工具，返回**一个**工具调用。
真实客户端走 function calling，离线客户端走确定性策略表；两者产出同构，
因此 trace、工具执行与断言逻辑完全复用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """模型不可用/鉴权失败/网络异常等，需要上层降级或终止。"""


@dataclass
class ToolInvocation:
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return str(self.args.get("reason") or "")


class LLMClient(ABC):
    mode: str = "unknown"

    def describe(self) -> str:
        """供结果与 trace 记录模型身份（不含密钥），用于事后核对是哪次运行。"""
        return self.mode

    def short_name(self) -> str:
        """提示符里显示的短标识，例如 `deepseek-chat` / `mock`。"""
        return self.mode

    @abstractmethod
    def decide(self, state: dict[str, Any], available_tools: list[str]) -> ToolInvocation | None:
        """返回下一步工具调用；返回 None 表示模型没有按协议给出工具调用。"""
