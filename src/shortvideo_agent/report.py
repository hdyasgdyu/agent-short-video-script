"""把 ScriptResult 渲染成可编辑的 Markdown 脚本稿，并负责产物落盘。"""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .models import ScriptResult
from .trace import render_trace_markdown, write_jsonl


def persist_run(
    result: ScriptResult,
    trace: list[dict],
    out_dir: Path,
    trace_dir: Path,
) -> dict[str, Path]:
    """写 4 个文件：结果 JSON、脚本 Markdown、trace JSONL、trace Markdown。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)

    suffix = "" if result.mode == "mock" else f"_{result.mode}"
    stem = f"{result.case_id}{suffix}"
    paths = {
        "json": out_dir / f"{stem}.json",
        "md": out_dir / f"{stem}.md",
        "trace_jsonl": trace_dir / f"{stem}.jsonl",
        "trace_md": trace_dir / f"{stem}.md",
    }

    try:
        result.trace_file = str(paths["trace_jsonl"].relative_to(config.ROOT)).replace("\\", "/")
    except ValueError:
        result.trace_file = str(paths["trace_jsonl"])

    paths["json"].write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["md"].write_text(render_script_markdown(result), encoding="utf-8")
    write_jsonl(paths["trace_jsonl"], trace)
    paths["trace_md"].write_text(
        render_trace_markdown(result.case_id, result.mode, trace, result.llm_info),
        encoding="utf-8",
    )
    return paths


def render_script_markdown(result: ScriptResult) -> str:
    script = result.script
    lines: list[str] = []
    title = script.title if script else "（无稿件）"
    lines.append(f"# {title}")
    lines.append("")
    meta = [
        f"状态 **{result.status.value}**",
        f"模式 `{result.mode}`",
        f"模型 `{result.llm_info or '-'}`",
        f"平台 `{script.platform if script else '-'}`",
        f"目标 {result.target_seconds or '-'} 秒",
        f"预计 **{result.estimated_total_seconds if result.estimated_total_seconds is not None else '-'}** 秒",
        f"步数 {result.steps}",
        f"工具调用 {result.tool_calls}",
    ]
    lines.append("> " + " ｜ ".join(meta))
    lines.append("")

    if script:
        lines.append("## 分镜脚本")
        lines.append("")
        lines.append("| 镜号 | 画面 | 旁白 / 字幕 | 预计时长(s) | 素材来源 |")
        lines.append("| --- | --- | --- | --- | --- |")
        per_shot = (result.duration.per_shot if result.duration else {}) or {}
        for shot in script.shots:
            refs = ", ".join(shot.fact_refs) if shot.fact_refs else "—"
            dur = per_shot.get(str(shot.shot_id), "-")
            lines.append(
                f"| {shot.shot_id} | {shot.visual} | {shot.narration} | {dur} | {refs} |"
            )
        lines.append("")
        total = result.estimated_total_seconds
        lines.append(f"**预计总时长：{total if total is not None else '-'} 秒**")
        lines.append("")
        if script.disclaimer:
            lines.append(f"免责声明：{script.disclaimer}")
            lines.append("")

    lines.append("## 事实来源核对")
    lines.append("")
    if result.fact_map:
        lines.append("| 素材 ID | 原文片段 |")
        lines.append("| --- | --- |")
        for mid, snippets in result.fact_map.items():
            lines.append(f"| {mid} | {' / '.join(snippets)} |")
    else:
        lines.append("未引用任何素材（本稿不含产品事实断言）。")
    lines.append("")

    lines.append("## 规则检查")
    lines.append("")
    if result.check:
        if result.check.ok:
            lines.append(f"通过（检查 {result.check.checked_shots} 个分镜）。")
        else:
            lines.append("| 级别 | 代码 | 说明 | 定位 |")
            lines.append("| --- | --- | --- | --- |")
            for v in result.check.violations:
                lines.append(f"| {v.severity} | {v.code} | {v.message} | {v.locator or '-'} |")
    else:
        lines.append("未执行规则检查。")
    lines.append("")

    lines.append("## 风险与说明")
    lines.append("")
    if result.risks:
        for r in result.risks:
            lines.append(f"- {r}")
    else:
        lines.append("- 无额外风险项。")
    lines.append("")

    lines.append("## 任务状态与消息")
    lines.append("")
    for m in result.messages:
        lines.append(m if m.startswith(("- ", "* ")) else f"- {m}")
    lines.append("")

    if result.mode == "mock":
        lines.append(
            "> 注意：本结果由离线 Mock 模型产出，仅用于验证流程、工具契约与护栏，"
            "不代表真实模型的决策质量。"
        )
        lines.append("")
    return "\n".join(lines)
