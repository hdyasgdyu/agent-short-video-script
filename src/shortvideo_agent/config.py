"""全局配置：路径、执行上限、平台口径。

所有上限都可以通过环境变量覆盖，便于演示“达到上限即返回未完成状态”。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
PRODUCT_DIR = DATA_DIR / "products"
RULE_FILE = DATA_DIR / "brand_rules.json"
PLATFORM_FILE = DATA_DIR / "platform_profiles.json"
DEFAULT_OUTPUT_DIR = ROOT / "examples" / "outputs"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# —— 执行上限（题目要求：必须有步数/工具调用次数上限）——
# 取值依据：真实模型单例实测 5~9 步，留出改稿余量后定为 15 步 / 12 次调用，
# 既能兜住失控，也不会误伤正常的“估算→改稿→复检”路径。
MAX_STEPS = _int_env("MAX_STEPS", 15)            # 模型决策轮数上限
MAX_TOOL_CALLS = _int_env("MAX_TOOL_CALLS", 12)  # 工具调用次数上限
MAX_REPAIR_ROUNDS = _int_env("MAX_REPAIR_ROUNDS", 2)  # 超时长/违规后的改稿轮数上限
MAX_SEARCH_ATTEMPTS = _int_env("MAX_SEARCH_ATTEMPTS", 3)  # 检索改写次数上限
# LangGraph 图级保护：每个 agent→tools 往返约消耗 2 个 super-step，
# 必须显著大于 2*MAX_STEPS，否则递归上限会先于业务上限触发（trace 会缺）。
RECURSION_LIMIT = _int_env("RECURSION_LIMIT", 60)

# 时长容差：估算模型存在 ±10% 误差，允许 1 秒超差
DURATION_TOLERANCE_SECONDS = 1.0
# 每个镜头的固定转场/留白
SHOT_PADDING_SECONDS = 0.3
# 首稿字数系数（mock 模型模拟“初稿偏长”：30s 目标会写出约 38s 的初稿）
DRAFT_OVERWRITE_RATIO = 1.27
# 修正稿的目标系数（留出余量，避免卡在容差边界）
REPAIR_BUDGET_RATIO = 0.95


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_platform_profiles() -> dict:
    return load_json(PLATFORM_FILE)


def load_brand_rules() -> dict:
    return load_json(RULE_FILE)


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 解析，避免为一个功能引入额外依赖。已存在的环境变量优先。"""
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
