"""Trace：把每次决策落成“观察 → 依据 → 后续动作 → 结果”。

存在两种形式：
- `<case>.jsonl`：机器可读，测试直接断言（model / tool 两条交替，便于逐条校验）；
- `<case>.md`：人可读，把一对 model+tool 合并成一个决策块，
  正好对应题目要求的「步骤、工具参数、返回摘要、后续动作、决策依据」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _format_action_args(args: dict[str, Any], limit: int = 300) -> str:
    args = dict(args or {})
    script = args.get("script")
    if isinstance(script, dict):
        shots = script.get("shots") or []
        args["script"] = f"<{len(shots)} 镜 / 标题={script.get('title')}>"
    text = json.dumps(args, ensure_ascii=False)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _one_line(text: str, limit: int = 320) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_trace_markdown(
    case_id: str,
    mode: str,
    entries: list[dict[str, Any]],
    model_info: str | None = None,
) -> str:
    decisions = [e for e in entries if e.get("actor") == "model"]
    tool_calls = [e for e in entries if e.get("actor") == "tool"]
    lines = [
        f"# 执行记录：{case_id}",
        "",
        f"- 模型模式：`{mode}`",
        f"- 模型端点：`{model_info or '-'}`",
        f"- 决策次数：{len(decisions)} ｜ 工具执行：{len(tool_calls)} 次",
        "",
        "每条记录四要素：**观察**（上一步工具返回）→ **依据**（模型给出的结论式理由，非思考过程）"
        "→ **后续动作**（选择的工具与参数）→ **结果**（该动作的返回摘要）。",
        "",
    ]

    i = 0
    while i < len(entries):
        entry = entries[i]
        if entry.get("actor") != "model":
            i += 1
            continue

        result = None
        if i + 1 < len(entries) and entries[i + 1].get("actor") == "tool":
            result = entries[i + 1]

        action = entry.get("next_action") or entry.get("tool") or "（未返回工具调用）"
        lines += [
            f"## 第 {entry.get('step')} 步 · {entry.get('phase')}",
            "",
            f"- **观察**：{_one_line(entry.get('reacts_to')) or '无（任务开始）'}",
            f"- **依据**：{entry.get('decision_basis') or '-'}",
            f"- **后续动作**：`{action}` `{_format_action_args(entry.get('args') or {})}`",
        ]
        if result is None:
            lines.append("- **结果**：（本轮未执行工具）")
        else:
            mark = "成功" if result.get("ok") else f"失败（{result.get('error')}）"
            lines.append(f"- **结果**：{mark} — {_one_line(result.get('result_summary'))}")
        lines.append("")

        i += 2 if result is not None else 1

    return "\n".join(lines)
