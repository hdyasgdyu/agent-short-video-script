"""真实模型自检：验证 .env 配置能否真正驱动一次 function calling。

只发一次最小请求，不跑完整流程，用来在花时间跑全量用例前排除
“key 错 / base_url 错 / 模型不支持 tools”这三类问题。

用法：
    set PYTHONPATH=src      # macOS/Linux: export PYTHONPATH=src
    python scripts/probe_model.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shortvideo_agent import config  # noqa: E402
from shortvideo_agent.llm.base import LLMError  # noqa: E402
from shortvideo_agent.llm.openai_client import OpenAICompatClient  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

    config.load_dotenv()
    base = os.getenv("OPENAI_BASE_URL", "")
    key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("MODEL_NAME", "")

    print("=" * 68)
    print(f"BASE_URL = {base or '(未设置)'}")
    print(f"MODEL    = {model or '(未设置)'}")
    print(f"API_KEY  = {('已设置（' + key[:4] + '…，共 ' + str(len(key)) + ' 字符）') if key else '(未设置)'}")
    print("=" * 68)

    if not base or not key or not model:
        print("\n[失败] 缺少配置。请把 .env.example 复制成 .env，并填写三项变量。")
        return 2

    state = {
        "phase": "research",
        "brief": {"topic": "露营投影仪怎么选", "platform": "douyin", "target_seconds": 30},
        "missing_fields": [],
        "materials": [],
        "observations": [],
        "search_queries": [],
        "script": None,
        "duration": None,
        "check": None,
        "steps": 0,
        "tool_calls": 0,
    }

    try:
        client = OpenAICompatClient()
    except LLMError as exc:
        print(f"\n[失败] {exc}")
        return 2

    started = time.time()
    try:
        invocation = client.decide(state, ["search_materials", "request_clarification"])
    except LLMError as exc:
        print(f"\n[失败] 模型请求异常：{exc}")
        print("排查建议：确认 BASE_URL 是否以 /v1 结尾、key 是否有效、网络是否可达。")
        return 1
    elapsed = time.time() - started

    print(f"响应耗时：{elapsed:.2f}s")
    if invocation is None:
        print("\n[警告] 模型没有返回工具调用（function calling 不可用或未命中）。")
        print("后果：Agent 拿不到动作，两次后会以 incomplete_limit_reached 结束。")
        print("建议：换用支持 tools 的模型（如 deepseek-chat / qwen-plus / glm-4-plus）。")
        return 1

    print(f"工具调用：{invocation.name}")
    print("参数：" + json.dumps(invocation.args, ensure_ascii=False))
    if invocation.name != "search_materials":
        print("[提示] 首轮没有按预期检索素材，可接受，跑用例时观察后续行为即可。")
    print("\n[通过] 模型可用，且返回了结构化工具调用。")
    print("下一步：python scripts/run_examples.py --mode llm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
