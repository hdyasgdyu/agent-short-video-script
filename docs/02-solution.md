# 02 方案与取舍

## 1. 调用流程

```
用户输入(JSON)
   │
   ▼
[INTAKE 校验]  硬必填缺失 → 只把「缺什么」写进 state，交给模型决定是否请求补充
   │
   ▼
[LangGraph: research]  模型 → search_materials(query) → 观察命中情况
   │                         ├ 命中：进入 draft
   │                         ├ 0 命中：模型改写 query 重试（≤3 次）
   │                         └ 仍 0 命中：request_clarification → needs_input
   ▼
[draft]   模型 → submit_script(完整脚本)，每条事实句带 fact_refs
   ▼
[verify]  模型 → estimate_duration() → 观察总时长/超差
   │            ├ 超时长：模型改稿 → submit_script → 回到 estimate（复检）
   │            └ 达标：check_rules() → 观察违规项
   │                     ├ 有 error：按项修正 → 复检
   │                     └ 通过：finalize_script() → completed
   └ 改稿轮数 > 2 → 模型收到“不再接受新版本”，转 report_unfixable → failed_unfixable
```

代码层面只有三个节点：`agent`（模型选动作）、`tools`（执行并回灌观察）、`abort`（上限兜底）。
**下一次进哪个节点、要不要结束，取决于模型输出的工具调用与工具返回结果**，没有写死的业务分支。

## 2. 为什么用 LangGraph（版本 1.2.19）

| 用到的能力 | 承担职责 |
| --- | --- |
| `StateGraph` + `TypedDict` state | 管理跨步骤状态（素材、稿件、时长、检查、计数、trace），避免全局变量 |
| 条件边 `add_conditional_edges` | 表达「模型动作 → 工具 → 回模型 / 结束 / 兜底」的循环 |
| `Annotated[list, operator.add]` | 观察与 trace 追加，不覆盖历史 |
| `config={"recursion_limit": 30}` | 图级兜底保护（业务上限仍是自己实现的步数/调用上限） |

明确的职责边界：**LangGraph 只负责“状态流转和循环”，业务决策全部由模型 + 工具观察驱动。**
评审若替换成 ADK 或原生循环，`tools/`、`models.py`、`llm/` 均不用改，只需重写 `agent/graph.py`。

## 3. ReAct 循环的具体落点

本项目的落点：

| 环节 | 实现 |
| --- | --- |
| 模型选择动作 | `LLMClient.decide(state, available_tools)` 返回 `ToolInvocation(name, args)`，真实模式走 function calling |
| 执行工具 | `registry.execute_tool()`，含阶段白名单、故障注入、异常转观察 |
| 观察结果 | `ToolResult.summary` 回灌为 `observations[-1]`，同时写进 trace 的 `result_summary` |
| 调整或结束 | 下一轮模型读 `observations` + `duration` + `check` 决定：继续改稿 / 复检 / 提交 / 请求补充 / 声明不可修 |

**与“预设流程”的边界**：阶段划分和工具白名单是骨架（允许），但“超时长后是删镜头还是压缩旁白”“检索无果后是换查询角度还是直接问用户”这些选择没有写在代码里，
由模型在 `reason` 字段给出依据，trace 可逐条核对。

## 4. 三个动态决策点（评分关键）

| 触发 | 模型可选动作 | 本项目实际观察到的行为 |
| --- | --- | --- |
| 检索 0 命中 | 改写 query / 放宽范围 / 请求补充 | `03_no_material`：3 次查询各不相同（原始主题 → 参数词 → 核心词），仍无果才请求补充 |
| 工具报错 | 同参重试 / 换参重试 / 跳过 / 终止 | `04_tool_failure`：首次超时后**换查询角度**重试并成功，trace 两条 search 参数不同 |
| 时长超标 | 删镜头 / 压旁白 / 声明无法满足 | `05_over_duration`：37.7s →（压缩保留事实句）→ 27.2s；`test_repair_limit_leads_to_explicit_failure` 覆盖声明失败 |

## 5. 工具契约

| 工具 | 输入 | 输出要点 | 为什么必须由工具做 |
| --- | --- | --- | --- |
| `search_materials` | query, top_k | material_id / snippet / score | 事实来源必须来自库，不能由模型生成 |
| `submit_script` | script | 校验结构、refs 合法性、改稿轮数 | 防止引用不存在的素材、防止无限改稿 |
| `estimate_duration` | — | 总时长、每镜时长、超差 | **唯一时长口径**，杜绝模型自报 |
| `check_rules` | — | violations（时长/禁用词/免责/事实/数字） | 规则判断不可交给模型自觉 |
| `finalize_script` | — | 守卫：未估算/未检查/未通过则拒绝 | 防止模型跳过验证直接交付 |
| `request_clarification` | questions | status=needs_input | 终止型动作 |
| `report_unfixable` | reason, best_effort | status=failed_unfixable | 「明确失败」必须是可观察动作 |

所有工具都带一个 `reason` 必填语义字段：作为 trace 里的“决策依据”，
只要求一句结论式说明，不要求也不记录思维过程。

## 6. 上限与终止

| 上限 | 默认值 | 触发结果 |
| --- | --- | --- |
| `MAX_STEPS` | 12 | `incomplete_limit_reached` + 当前进度 |
| `MAX_TOOL_CALLS` | 10 | 同上 |
| `MAX_REPAIR_ROUNDS` | 2 | 工具拒绝新版本，模型转 `report_unfixable` |
| `MAX_SEARCH_ATTEMPTS` | 3 | 模型转 `request_clarification` |
| `RECURSION_LIMIT` | 30 | LangGraph 兜底，异常转为未完成状态 |

所有终止路径都会写 `result_messages`，包含“有没有稿、最近一次估算、检查是否通过”，
不存在只返回一个错误码的情况。

## 7. 防编造的三道闸门

1. **入口闸**：`submit_script` 拒绝引用未检索到的 material_id（`invalid_fact_refs`）。
2. **规则闸**：`check_rules` 检查每个镜头——
   - 出现“支持/续航/分辨率…”等断言词却无 `fact_refs` → `UNSUPPORTED_CLAIM`；
   - 出现“数字+量纲”（如 `80%`、`8 小时`、`1080P`）而引用原文不含该数字 → `*_CLAIM_UNSOURCED`；
   - 引用了不存在的 ID → `INVALID_FACT_REF`。
3. **交付闸**：`finalize_script` 在检查未通过时拒绝交付。

## 8. 技术选型与被否决的选项

| 决策 | 选择 | 否决理由（被否决的方案） |
| --- | --- | --- |
| 编排 | LangGraph 1.2.19 | 手写循环也可行且更快，但本项目选了框架以验证“框架不改变决策逻辑”；框架只承担状态流转 |
| 模型接入 | OpenAI 兼容 HTTP（httpx 直连） | 绑定单一厂商 SDK 会无法用其他厂商的 key 验证 |
| 结构化输出 | Pydantic 校验 + 工具参数 schema | 纯提示词要求 JSON 在真实模型上不稳定，容易退化成“解析失败” |
| 检索 | 纯 Python 2-gram 打分 | 向量库需要额外依赖与模型，且难以精确构造“0 命中”验收场景 |
| trace | 自写 JSONL + Markdown | langfuse/langsmith 需账号与网络，提交后不可复现 |
| 运行方式 | CLI |  CLI 最快闭环，且便于测试断言 |

## 9. 交互层（chat / REPL）

`src/shortvideo_agent/repl.py` 只是输入输出外壳，**不改变决策图**：

| 关注点 | 做法 | 为什么不影响可测性 |
| --- | --- | --- |
| 输入 | 自然语言 → 正则抽取 `topic/platform/target_seconds/product_ids` | 解析函数 `parse_brief` 是纯函数，单测覆盖；抽不到的字段交回 Agent 反问 |
| 多轮 | `last_status=needs_input` 时把下一句当“字段补充”，不覆盖原主题 | 由 `last_status` 判断，逻辑在 `handle_turn`，可单测 |
| 实时性 | `run_case(on_step=…)` 内部用 `app.stream(stream_mode="values")` 拿每步 state，diff 出新增 trace 条目回调 | 传 `on_step=None` 时仍走 `invoke`，批量路径与测试行为完全一致 |
| 产物 | 仍走 `persist_run()`，与 CLI 同一套文件 | 交互产出与批量产出结构相同，可互相复现 |

取舍：**不额外调用模型做意图解析**。多一次 LLM 调用能换来更好的槽位抽取，
但在“主题+时长+平台”这种结构简单的场景里，正则的准确率足够、延迟为零、成本为零，且可离线测试。
模型成本的额度留给真正需要推理的部分（写稿、判断是否改写查询、是否继续修正）。

## 10. 数据设计

- `data/products/P01.md`：**只有一条事实**“支持字幕”，用于复刻题目参考实例，也用来验证“素材少时不得编造”。
- `P02~P04`：分别提供 3~5 条可引用参数，让正常路径有足够依据。
- `brand_rules.json`：禁用词、必需免责声明、断言触发词、效果数据白名单（空）。
- `platform_profiles.json`：把“语速”外置成配置（抖音 5.5 字/秒、视频号 5.0、B站 4.8、小红书 5.2）。

平台设计的两个要点：

1. **清单由配置驱动**，不在代码里硬编码。新增平台只加一项配置，`--platform`、交互模式的中文别名、
   `check` 子命令会自动跟上；`python -m shortvideo_agent platforms` 可直接查看。
2. **未知平台直接报错，不静默降级**。之前实现是 `profiles.get(platform) or profiles["douyin"]`，
   这会让“平台写错”变成“用抖音语速算视频号”，时长达标就成了假象。
   改成抛 `ValueError` 后，CLI 会给出可用平台列表；若绕过入口进入工具层，
   模型会看到“平台未配置”这条观察并转为请求补充——这本身也是一次合法的决策。
