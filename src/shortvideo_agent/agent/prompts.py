"""真实模型模式的提示词。

原则：把硬约束写成可执行规则，把状态压缩成结构化事实，
不向模型灌输“应该怎么想”，只告诉它有哪些工具、当前观察到什么。
"""

from __future__ import annotations

import json
from typing import Any

from .. import config
from ..tools import describe_tools

SYSTEM_TEMPLATE = """你是短视频脚本 Agent，为创作者产出有事实依据、满足时长限制、可编辑的脚本。

硬约束（违反即视为失败）：
1. 只能通过调用工具推进任务，禁止输出自然语言正文。
2. 旁白中的每个产品事实（参数、功能、数据）都必须绑定 fact_refs，且只能引用 search_materials 实际返回的 material_id。
3. 严禁编造素材中不存在的信息，包括效果数据（如“效率提升 80%”）、绝对化用词、未经验证的对比结论。
4. 每次调用工具必须在 reason 字段写一句结论式的决策依据（不是思考过程）。
5. 修改稿件后必须重新调用 estimate_duration 与 check_rules，不能直接 finalize。
6. 检索 0 命中时可以改写 query 重试，仍无结果则调用 request_clarification。
7. 无法同时满足时长与规则时调用 report_unfixable 并说明原因，不要假装完成。

当前阶段：{phase}
本阶段可用工具：
{tools}

执行预算：已用 {steps}/{max_steps} 步，{tool_calls}/{max_tool_calls} 次工具调用；改稿上限 {max_repair} 轮。
"""


def build_system_prompt(state: dict[str, Any]) -> str:
    return SYSTEM_TEMPLATE.format(
        phase=state.get("phase"),
        tools=describe_tools(state.get("phase", "research")),
        steps=state.get("steps", 0),
        max_steps=config.MAX_STEPS,
        tool_calls=state.get("tool_calls", 0),
        max_tool_calls=config.MAX_TOOL_CALLS,
        max_repair=config.MAX_REPAIR_ROUNDS,
    )


def build_user_prompt(state: dict[str, Any]) -> str:
    brief = state.get("brief") or {}
    materials = state.get("materials") or []
    observations = state.get("observations") or []
    lines = ["【用户简报】", json.dumps(brief, ensure_ascii=False, indent=2)]

    if state.get("missing_fields"):
        lines += ["", f"【缺失必填字段】{state['missing_fields']}（继续生成只能靠猜测）"]

    lines += ["", "【已检索素材】"]
    if materials:
        for m in materials:
            lines.append(f"- {m['material_id']}（{m['product_name']}）：{m['snippet']}")
    else:
        lines.append("- 暂无。必须先调用 search_materials 获取依据。")

    if state.get("script"):
        lines += ["", "【当前稿件】", json.dumps(state["script"], ensure_ascii=False, indent=2)]
    if state.get("duration"):
        lines += ["", "【最近一次时长估算】", json.dumps(state["duration"], ensure_ascii=False)]
    if state.get("check"):
        lines += ["", "【最近一次规则检查】", json.dumps(state["check"], ensure_ascii=False, indent=2)]

    lines += ["", "【最近工具观察】"]
    if observations:
        for obs in observations[-4:]:
            flag = "成功" if obs.get("ok") else "失败"
            lines.append(f"- [{flag}] {obs.get('tool')}: {obs.get('summary')}")
    else:
        lines.append("- 无")

    lines += ["", f"【已尝试的检索词】{state.get('search_queries') or []}"]
    lines += ["", "请给出下一步的单个工具调用。"]
    return "\n".join(lines)
