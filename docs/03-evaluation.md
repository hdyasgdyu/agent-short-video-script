# 03 评测与结果

## 1. 测试概览

命令：`python -m pytest -q`
实际结果：**56 passed**（全文见 [examples/test-report.txt](../examples/test-report.txt)）

| 文件 | 覆盖内容 | 数量 |
| --- | --- | --- |
| `tests/test_tools.py` | 时长估算公式、检索命中/空、规则检查、阶段白名单、故障注入、提交守卫 | 14 |
| `tests/test_acceptance_paths.py` | 5 条验收路径 + P01 参考实例 + 改变时长对照 + trace 完整性 | 13 |
| `tests/test_limits.py` | 步数上限、调用上限、模型不返回工具、模型不可用、改稿上限→明确失败 | 5 |
| `tests/test_no_fabrication.py` | 百分比效果数据、无依据断言、篡改数字、禁用词、非法引用 | 6 |
| `tests/test_repl.py` | 自然语言解析（主题/时长/平台/产品）、反问后补充不覆盖主题、流式回调完整性、提示符模型标识 | 13 |
| `tests/test_cli.py` | 参数模式产物落盘、缺参返回码、check 通过/拦截 | 5 |

测试全部离线：Mock 模型 + 本地素材，不访问网络、不需要密钥。

## 2. 示例运行结果

复现命令：`python scripts/run_examples.py`

| 用例 | 状态 | 预计时长 | 步数/调用 | 关键事实 |
| --- | --- | --- | --- | --- |
| `01_normal` | completed | 28.0s | 5 / 5 | 初稿即达标，一次通过，引用 P02-F1/F2/F3 |
| `02_missing_info` | needs_input | — | 1 / 1 | 只调用了 `request_clarification`，未产生脚本 |
| `03_no_material` | needs_input | — | 4 / 4 | 3 次查询各不相同，全部 0 命中后请求补充 |
| `04_tool_failure` | completed | 28.0s | 6 / 6 | 首次检索注入超时 → 换查询角度 → 成功 |
| `05_over_duration` | completed | 27.2s | 7 / 7 | 首稿 37.7s → 压缩 → 复检 27.2s |
| `06_reference_p01` | completed | 27.4s | 7 / 7 | 首稿 38.0s → 修正 27.4s，仅引用 P01-F1 |
| `07_tight_duration` | completed | 18.3s | 7 / 7 | 与 01 同主题同素材，只把目标改成 18s：21.7s → 压缩 → 18.3s |

### 2.1 对照实验：改变时长，动作是否跟着变

这是评分点「改变时长或检索结果，核对下一步动作是否调整」的直接证据：

| 对比 | 01_normal（30s） | 07_tight_duration（18s） |
| --- | --- | --- |
| 工具序列 | search → submit → estimate → check → finalize | search → submit → estimate → **submit → estimate** → check → finalize |
| 估算次数 | 1 | 2 |
| 是否出现“超出目标” | 否 | 是（21.7s 超 3.7s） |
| 步数 | 5 | 7 |

同一主题、同一素材库、同一模型，只有 `target_seconds` 不同，模型就多走了一轮「压缩 → 复检」。
该差异被 `test_shorter_target_changes_next_action` 固化为断言。

### 2.2 真实模型（DeepSeek）实测

使用用户环境中的 `deepseek-flash` 端点跑同一批用例，产物带 `_llm` 后缀，不覆盖 Mock 结果：

| 用例 | 状态 | 时长 | 步数 | 观察 |
| --- | --- | --- | --- | --- |
| `01_normal_llm` | completed | 23.7s | 8 | 模型自己控制字数，一次通过 |
| `02_missing_info_llm` | needs_input | — | 2 | 先检索了一次才反问（Mock 是直接问） |
| `03_no_material_llm` | needs_input | — | 4 | 改写查询后请求补充 |
| `04_tool_failure_llm` | completed | 25.6s | 9 | 工具失败被观察到并换参重试 |
| `05_over_duration_llm` | completed | 29.0s | 8 | 初稿即进入容差，走的是「规则违规 → 改稿 → 复检」 |
| `06_reference_p01_llm` | completed | 24.2s | 8 | 仍只引用 P01-F1，无编造参数 |

真实模型与 Mock 的三点差异（都写进了各 `_llm` trace）：

1. **真实模型会先做事再问**：`02` 缺平台与时长，它仍先检索一次拿到素材，再请求补充；
   Mock 的策略表是「缺字段 → 直接问」。两者都不算错，但真实模型更倾向先收集信息。
2. **真实模型自己就会控时长**：`05/06` 的初稿直接落在 30 秒容差内，因此**没有触发压缩**。
   超时长修正这条路径由 Mock 的 `05`（37.7→27.2）和 `07`（21.7→18.3）覆盖。
3. **真实模型会自己修规则违规**：`05_llm` 的初稿踩了禁用词「第一」、缺免责声明、镜 6 断言无引用，
   模型在 `check_rules` 返回 3 项 error 后逐条修正并重新提交（旁白 160→158 字），复检通过。
   这条是「工具结果驱动动作调整」在真实模型上最硬的证据，见 `examples/trace/05_over_duration_llm.md`。

产物位置：

- 脚本稿：`examples/outputs/<case>.md`（可直接改，改完用 `check` 子命令复检）
- 结果 JSON：`examples/outputs/<case>.json`
- 执行记录：`examples/trace/<case>.jsonl`（机器读）与 `examples/trace/<case>.md`（人读）

## 3. 验收点 → 证据

### A1 正常生成

- 证据：[examples/outputs/01_normal.md](../examples/outputs/01_normal.md)、[examples/trace/01_normal.md](../examples/trace/01_normal.md)
- 断言：`status=completed`、5 个分镜、时长 28.0s ≤ 30+1、`fact_map` 非空、
  每个 `fact_refs` 都在已检索素材集合内（`test_normal_generation_every_fact_is_traceable`）。

### A2 缺关键信息

- 证据：[examples/outputs/02_missing_info.json](../examples/outputs/02_missing_info.json)
- 实际输出消息：

  > 需要你补充信息：缺少必填字段 ['platform', 'target_seconds']，继续生成会变成猜测，先请用户补充。
  > - 请提供发布平台：发布平台，例如 douyin / shipinhao / bilibili / xiaohongshu
  > - 请提供目标时长：目标时长（秒），例如 30

- 断言：`tool_calls == 1`，且唯一调用是 `request_clarification`，`script is None`。

### A3 无素材（检索无结果 → 改写 → 请求补充）

- 证据：[examples/trace/03_no_material.md](../examples/trace/03_no_material.md)
- 三次查询依次为：`量子猫砂盆的AI健康监测` → `…参数 规格` → `量子猫砂盆的 说明`
- 断言：查询互不相同、`search_attempts == 3`、最后一次工具是 `request_clarification`、无脚本产出。

### A4 工具失败（观察 → 调整）

- 证据：[examples/trace/04_tool_failure.md](../examples/trace/04_tool_failure.md)
- 逐步：

  | 步 | 动作 | 结果 |
  | --- | --- | --- |
  | 1 | `search_materials("露营投影仪怎么选")` | 失败：注入故障 `error_once`，上游超时 |
  | 2 | `search_materials("露营投影仪怎么选 参数 规格")` | 命中 4 条 |
  | 3~6 | 成稿 → 估算 → 检查 → 终稿 | completed 28.0s |

- 断言：存在 `ok=False` 的工具记录，且后续同一工具调用的参数与首次不同
  （`test_tool_failure_is_observed_then_recovered`）。

### A5 超时长修正

- 证据：[examples/trace/05_over_duration.md](../examples/trace/05_over_duration.md)
- 逐步：

  | 步 | 动作 | 结果摘要 |
  | --- | --- | --- |
  | 3 | `estimate_duration` | 37.7s，超目标 7.7s |
  | 4 | `submit_script`（压缩稿） | 旁白 228 字 → 162 字，事实句保留 |
  | 5 | `estimate_duration` | 27.2s，进入容差 |
  | 6~7 | `check_rules` → `finalize_script` | 通过并交付 |

- 断言：`estimate_duration` 至少 2 次；修正动作的 `reacts_to` 含“超出目标”；
  最终时长 ≤31；`check.ok=True`（`test_over_duration_is_compressed_and_rechecked`）。
- 补充证据：
  - `07_tight_duration`（18 秒）：21.7s → 18.3s，见 [examples/trace/07_tight_duration.md](../examples/trace/07_tight_duration.md)；
  - 明确失败：`test_repair_limit_leads_to_explicit_failure`（目标 5 秒）→ `failed_unfixable`。

### A6 达到上限

- 证据：[examples/test-report.txt](../examples/test-report.txt) 中 `tests/test_limits.py`
- `test_step_limit_returns_incomplete`：把 `MAX_STEPS` 调成 3，让模型持续检索 → `incomplete_limit_reached`
- `test_tool_call_limit_returns_incomplete`：把 `MAX_TOOL_CALLS` 调成 2 → 同样收敛
- `test_repair_limit_leads_to_explicit_failure`：目标 5 秒（物理上放不下 5 个分镜）→
  改稿 2 轮后工具拒绝，模型转 `report_unfixable` → `failed_unfixable` 并给出“最接近方案”

### A7 防编造

- 证据：`tests/test_no_fabrication.py`
- 覆盖：`效率提升 80%`（百分比无出处）、`支持实时字幕`（无 refs）、
  `续航 8 小时`（原文是 2.5 小时）、`市面最好`（禁用词）、`P01-F9`（不存在的素材）。

### A8 参考实例（题目给的 P01 场景）

- 证据：[examples/outputs/06_reference_p01.md](../examples/outputs/06_reference_p01.md)
- 素材库中 P01 只有一条事实：`[P01-F1] 支持字幕`。
- 实际过程：首稿估算 **38.0s**（超出 8.0s）→ 压缩 → 复检 **27.4s** → 终稿 5 个分镜，
  `fact_map` 只有 `{"P01-F1": ["支持字幕"]}`。
- 全篇没有出现任何 P01 未提供的参数（没有分辨率、没有续航、没有“效率提升 xx%”）。

## 4. Mock 与真实模型的边界

离线 Mock 的“决策”来自 `src/shortvideo_agent/llm/mock_client.py` 里的策略表，
“写作/压缩”来自 `llm/drafting.py` 的确定性算法。因此：

| 只能靠 Mock 证明 | 只能靠真实模型证明 |
| --- | --- |
| 工具契约与参数校验有效 | 模型会不会主动改写查询 |
| 上限、终止、状态机不会漏出口子 | 写出来的文案质量 |
| trace 记录完整、可复现 | 压缩策略是否合理（是否保留事实句） |
| 规则检查能拦住编造 | 模型是否会换个说法绕过规则 |

本仓库两套记录都提交了：

- `examples/outputs/*.json|md`、`examples/trace/*.jsonl|md`：Mock，可离线复现；
- `examples/outputs/*_llm.*`、`examples/trace/*_llm.*`：DeepSeek 真实模型实测。

每条 trace 的头部都写明 `模型端点`，不会混淆。评审用自己 key 可重跑真实模式：
`python scripts/run_examples.py --mode llm`，产物同样带 `_llm` 后缀，不覆盖 Mock。

**结论上的克制**：Mock 能证明“流程和护栏对”，真实模型能证明“这个模型确实会按观察调整动作”。
但两者都不能证明“这套脚本在真实平台上有效果”——所以本项目的产出里没有任何效果数据结论。

## 5. 已知失败与限制

1. **时长精度**：`estimate_duration` 按字数/语速估算，实际成片可能有 ±10% 偏差；容差设为 1 秒，
   且该风险写进每次交付的 `risks`。
2. **压缩可能过度**：`05_over_duration` 修正到 27.2s（目标 30s），比目标低约 9%，
   因为压缩按“预算系数 + 整句删除 + 短句补齐”，不是最优解。这是刻意留的安全余量。
3. **Mock 文案质量有限**：为凑字数会重复使用语气句，这是 Mock 的填充策略，不代表真实模型措辞。
4. **真实模型的 30 秒目标不会触发超时长**：`05_llm`/`06_llm` 的初稿直接落在容差内，
   所以“超时长 → 压缩”在真实模型上未发生；已用 `07_tight_duration`（18 秒）与 Mock 的 `05` 覆盖该路径。
5. **真实模型缺信息时仍会先检索一次**（`02_llm`）：最终状态正确，但不如 Mock 干脆。
   这是模型行为而非代码缺陷，未做强制干预（强制限制工具就退化成固定分支了）。
6. **数字校验只看量纲**：`数字 + 单位` 才视为数据断言，`S2`、`X1` 这类型号数字不会误伤，
   代价是“纯数字断言”（如“三代”）不会被拦截。
7. **事实语义一致性**：规则只能做到“原文包含该数字/断言词”，无法判断语义是否真的等价，仍需人工终审。
8. **图递归兜底**：若 `RECURSION_LIMIT` 先于业务上限触发，`GraphRecursionError` 分支只能用最后一次
   state 组装结果，trace 可能不完整；默认参数下（上限 12 步 vs 递归 30）不会发生，测试验证了业务上限优先。
9. **交互模式的槽位抽取是正则**：`"帮我写个…"` 这类句式能正确抽出主题，但复杂嵌套表达可能抽不干净，
   此时会退化成“把整句当主题”，或由 Agent 反问。

## 6. 复现步骤

```bash
pip install -r requirements.txt
python scripts/run_examples.py          # 产出 7 组 outputs + trace
python -m pytest -q                     # 56 个测试
python -m shortvideo_agent check -f examples/outputs/05_over_duration.json   # 复检任意脚本
```

`check` 子命令可以对你手工改过的脚本重新估算与检查，退出码 0 表示通过、1 表示不通过，
方便接进 CI。
