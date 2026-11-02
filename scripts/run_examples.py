"""批量重跑 examples/inputs 下的全部用例，产出 outputs/ 与 trace/。

用法：
    python scripts/run_examples.py            # 离线 mock
    python scripts/run_examples.py --mode llm # 真实模型（需 .env）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shortvideo_agent.cli import main  # noqa: E402


def main_entry() -> int:
    mode = "mock"
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]

    inputs = sorted((ROOT / "examples" / "inputs").glob("*.json"))
    if not inputs:
        print("examples/inputs 下没有用例")
        return 1

    for path in inputs:
        main(["run", "-i", str(path), "--mode", mode])
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
