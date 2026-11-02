# 短视频脚本 Agent

给定主题、受众、平台、目标时长与风格，Agent 会：检索产品素材 → 产出分镜脚本 →
用工具核算时长与规则 → 不达标就改稿复检 → 输出**可编辑脚本 + 事实来源 ID + 风险 + 任务状态**。

核心约束：**旁白里的每个产品事实都必须能追到素材 ID**，编造参数会被规则检查拦下。

---

## 1. 快速开始（离线，无需任何 API Key）

```bash
pip install -r requirements.txt

# 跑全部 6 组示例，产出 examples/outputs/*.md|json 与 examples/trace/*.jsonl|md
python scripts/run_examples.py

# 跑测试（56 个，全离线）
python -m pytest -q
```

跑单个用例：

```bash
# Windows
set PYTHONPATH=src
# macOS / Linux
export PYTHONPATH=src

python -m shortvideo_agent run -i examples/inputs/06_reference_p01.json
```

对已生成的脚本（或人工改过的脚本）重新检查时长与规则：

```bash
python -m shortvideo_agent check -f examples/outputs/06_reference_p01.json
```

### 交互模式（像 Claude Code 那样边聊边改）

```cmd
set PYTHONPATH=src
python -m shortvideo_agent chat
```

不带子命令直接 `python -m shortvideo_agent` 也会进入交互模式。

实际跑一轮长这样（每步工具调用实时打印，不是跑完才吐结果）：

```
  短视频脚本 Agent  ·  交互模式
  模型 api.deepseek.com / deepseek-chat

你 [deepseek-chat] › 帮我写个露营投影仪的 30 秒抖音脚本
  · 露营投影仪 | douyin | 30s

  ⚙ search_materials query="露营投影仪"                      0.0s
    ↳ 命中 5 条素材：P02-F4=整机重量 0.6 千克; P02-F5=支持自动梯形校正; ...
  ⚙ submit_script 5 镜 / 227 字                               0.0s
    ↳ 已接收初稿（5 镜，旁白 227 字）…
  ⚙ estimate_duration                                       0.0s
    ↳ 估算总时长 37.5s，超出目标 30s 共 7.5s …
  ⚙ submit_script 5 镜 / 167 字                              0.0s
  ⚙ estimate_duration                                       0.0s
    ↳ 估算总时长 27.5s / 目标 30s，在容差内 …
  ⚙ check_rules → finalize_script

  ✔ completed · 27.5s / 30s · 5 镜 · 素材 P02-F1,P02-F2,P02-F3
  露营投影仪
   1   5.6s  你有没有遇到过露营投影仪的纠结时刻？…
   2   5.4s  便携投影仪 S2 支持 1080P 物理分辨率。…
```

交互命令：

提示符里的方括号就是**当前实际使用的模型**（`[deepseek-chat]` / `[mock]`），
启动时也会打印完整端点；`/mode` 切换后会立即重新显示。

| 命令 | 作用 |
| --- | --- |
| `/help` | 命令列表 |
| `/model` | 查看当前模式、模型端点、简报与上一轮状态 |
| `/mode auto\|mock\|llm` | 切换模型，`auto` 会优先真实模型、配置不可用则降级 mock |
| `/set target_seconds=45 platform=xiaohongshu` | 手动改字段，不用重说一遍需求 |
| `/show` | 重打上一版脚本 |
| `/trace` | 打印执行记录路径 |
| `/reset` | 清空会话 |
| `/quit` | 退出 |

补充说明：

- 缺信息时 Agent 会反问，直接回「30 秒，抖音」即可继续，**不会把补充句当成新主题**。
- 说「时长改成 20 秒」这类话会覆盖字段后重跑。
- 交互里的自然语言解析是轻量正则（不额外消耗模型调用），真正的决策仍然由模型完成。
- Windows 若中文输入异常，先 `chcp 65001` 切到 UTF-8。

### 用自己的主题跑（不必写 JSON，单次命令）

```cmd
:: 最简：主题 + 目标时长
python -m shortvideo_agent run --topic "露营投影仪怎么选" --seconds 30 --mode llm

:: 完整参数
python -m shortvideo_agent run --topic "会议耳机字幕功能体验" --platform douyin ^
    --seconds 30 --audience "线上会议频繁的上班族" --style "真实体验分享" ^
    --products P01 --mode llm
```

| 参数 | 说明 |
| --- | --- |
| `--topic` | 主题（必填） |
| `--seconds` | 目标时长秒数；不给则走 `needs_input`，由 Agent 向你提问 |
| `--platform` | `douyin`（默认）/ `shipinhao` / `bilibili` / `xiaohongshu` |
| `--audience` / `--style` | 受众与风格（可选） |
| `--products` | 限定素材范围，逗号分隔，如 `P01,P02` |
| `--case-id` | 自定义用例名，决定输出文件名 |
| `--mode` | `mock`（默认，离线）/ `llm`（真实模型） |

产物写到 `outputs/`，不污染 `examples/` 下的归档：

```
outputs/adhoc_1102_153034.md      # 给人看的脚本稿
outputs/adhoc_1102_153034.json    # 给程序用的结构化结果
outputs/trace/adhoc_1102_153034.md   # 一步步的执行记录（谁在什么时候调了什么、为什么）
```

### 用自己的产品素材

| 要改的东西 | 位置 | 格式 |
| --- | --- | --- |
| 产品素材 | `data/products/P05.md` | 一行一条事实：`- [P05-F1] 支持 4K 输出`，ID 必须唯一 |
| 品牌规则 | `data/brand_rules.json` | 禁用词、必需免责声明、标题字数、最少分镜数 |
| 平台语速 | `data/platform_profiles.json` | 每个平台的 `chars_per_second`，决定时长估算口径 |

素材是第一等公民：Agent 只能引用 `search_materials` 返回过的 `material_id`，
库里没有的事实它写不出来（会被 `check_rules` 拦下）。

### 平台不是写死的

可用平台由 `data/platform_profiles.json` 驱动，查看现有配置：

```bash
python -m shortvideo_agent platforms
```

```
标识          名称             语速(字/秒)       时长范围(秒)      标题上限
bilibili    B站                 4.8        20-600        30
douyin      抖音                 5.5        15-180        20
shipinhao   视频号                5.0        15-300        24
xiaohongshu 小红书                5.2        15-120        20
```

**新增平台（如快手）只需在 `data/platform_profiles.json` 加一项，不用改代码**：

```json
"kuaishou": {
  "name": "快手",
  "chars_per_second": 5.3,
  "target_seconds_range": [15, 180],
  "title_max_chars": 20
}
```

加了之后：

- `--platform kuaishou` 直接可用；
- 交互模式里说「快手」也能被识别（别名从配置的 `name` 字段读取）；
- 语速决定时长估算口径，写错平台会**明确报错**而不是静默套用抖音参数。

> 也可以用 `pip install -e .` 安装后直接使用 `shortvideo-agent run -i ...`。

## 2. 真实模型模式（需要 API Key）

### 2.1 配置

```bash
# 复制模板（Windows 用 copy）
cp .env.example .env
```

编辑 `.env`（该文件已在 `.gitignore` 中，不会被提交）：

```ini
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-你的真实key
MODEL_NAME=deepseek-chat
```

常用 OpenAI 兼容端点：

| 供应商 | `OPENAI_BASE_URL` | `MODEL_NAME` 示例 |
| --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| Ollama（本地） | `http://localhost:11434/v1` | `qwen2.5:7b` |

要求：模型必须支持 **function calling（tools）**。DeepSeek 的 `deepseek-reasoner` 等推理模型
对 tools 支持不稳定，建议先用 `deepseek-chat` 验证链路。

Ollama 本地模式需要一个非空的 key（内容随意，Ollama 不校验）：
`set OPENAI_API_KEY=ollama`。

**也可以不创建 `.env`，直接使用环境变量。** 优先级：

```
进程环境变量  >  .env 文件  >  代码默认值
```

（`.env` 只补空缺，不会覆盖已存在的环境变量。）

```cmd
:: Windows 当前窗口有效
set OPENAI_BASE_URL=https://api.deepseek.com/v1
set OPENAI_API_KEY=sk-你的真实key
set MODEL_NAME=deepseek-chat

:: Windows 永久（写入用户变量，需重开终端）
setx OPENAI_API_KEY "sk-你的真实key"
```

```powershell
# PowerShell
$env:OPENAI_API_KEY = "sk-你的真实key"
```

```bash
# macOS / Linux / CI
export OPENAI_API_KEY=sk-你的真实key
```

验证是否读到了（不会真的发请求）：

```bash
python -c "import sys;sys.path.insert(0,'src');from shortvideo_agent import config;from shortvideo_agent.llm.openai_client import OpenAICompatClient;config.load_dotenv();c=OpenAICompatClient();print(c.base_url,c.model,len(c.api_key))"
```

### 2.2 先自检，再跑用例

```bash
# Windows
set PYTHONPATH=src
# macOS / Linux
export PYTHONPATH=src

python scripts/probe_model.py
```

它会发一次最小请求，只验证三件事：端点可达、key 有效、模型能返回**结构化工具调用**。
输出 `[通过]` 再往下跑，否则先按 2.5 排错。

### 2.3 跑真实模型用例

```bash
# 全部 6 组
python scripts/run_examples.py --mode llm

# 单个用例
python -m shortvideo_agent run -i examples/inputs/06_reference_p01.json --mode llm
```

产物会带 `_llm` 后缀，**不会覆盖** Mock 结果：

```
examples/outputs/06_reference_p01_llm.md
examples/outputs/06_reference_p01_llm.json
examples/trace/06_reference_p01_llm.jsonl
examples/trace/06_reference_p01_llm.md
```

### 2.4 会发多少次请求

单例 5~9 次模型请求，次数等于该用例的「决策次数」，可直接在 trace 头部核对：

```
- 决策次数：7 ｜ 工具执行：7 次
```

本项目**没有做 token 计量**，因此不给出金额结论——没有实测依据的成本数字和编造没有区别。

### 2.5 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `真实模型模式需要 OPENAI_BASE_URL 与 OPENAI_API_KEY` | `.env` 没创建或变量名写错 | 确认文件在仓库根目录、变量名完全一致 |
| `模型返回 401` | key 错误或已失效 | 换 key 重试 |
| `模型返回 404` | `BASE_URL` 少了 `/v1` 或路径不对 | 按 2.1 的表格填 |
| `模型返回 400 ... tools` | 该模型不支持 function calling | 换支持 tools 的模型 |
| `[警告] 模型没有返回工具调用` | 模型只回自然语言 | 换模型；或用 `--mode mock` 验证流程 |
| 跑用例时 `incomplete_limit_reached`，trace 里 `tool: null` | 模型两次都没给出工具调用 | 同上，这是护栏生效，不是崩溃 |
| 网络超时 | 需要代理 | 设置 `HTTPS_PROXY` 环境变量后重试 |

> 未配置 key 时请使用默认的 `--mode mock`。两种模式的差别见 [docs/02-solution.md](docs/02-solution.md)，
> Mock 的能力边界见 [docs/03-evaluation.md](docs/03-evaluation.md) 第 4 节。

## 3. 目录结构

```
.
├── README.md
├── requirements.txt / pyproject.toml
├── .env.example
├── src/shortvideo_agent/
│   ├── cli.py               # chat / run / check 子命令
│   ├── repl.py              # 交互式会话：自然语言进，逐步打印工具调用
│   ├── config.py            # 执行上限、平台语速口径、路径
│   ├── models.py            # Pydantic 契约（输入/工具返回/交付物）
│   ├── agent/
│   │   ├── graph.py         # LangGraph 决策图，含上限与终止
│   │   └── prompts.py       # 真实模型提示词
│   ├── llm/
│   │   ├── base.py          # LLMClient 协议
│   │   ├── openai_client.py # OpenAI 兼容 function calling
│   │   ├── mock_client.py   # 离线策略表
│   │   └── drafting.py      # Mock 的成稿/压缩能力替身
│   ├── tools/               # 检索/估算/检查/提交/终稿/请求补充/声明不可修
│   ├── trace.py             # JSONL + Markdown 执行记录
│   └── report.py            # 脚本稿渲染
├── data/
│   ├── products/P01..P04.md     # 可引用素材（P01 只有“支持字幕”）
│   ├── brand_rules.json         # 禁用词、免责声明、防编造判据
│   └── platform_profiles.json   # 平台语速与时长口径
├── tests/                   # 56 个离线测试
├── examples/
│   ├── inputs/              # 6 组输入
│   ├── outputs/             # 实际输出（.md + .json）
│   ├── trace/               # 实际执行记录（.jsonl + .md）
│   └── test-report.txt      # pytest -v 实际输出
└── docs/01..05
```

## 4. 它是怎么决策的

```
START → agent ──(模型选动作)──→ tools ──(观察结果)──→ agent
          │                        │
          │                        └─ 结果写入 state，改变阶段/稿件/检查状态
          ├─ 状态已定 → END
          └─ 步数或调用超限 → abort → END（返回 incomplete_limit_reached）
```

- **骨架是状态机**：`research → draft → verify`，每个阶段有工具白名单，防止模型在错误时机调错工具。
- **决策是模型的**：选哪个工具、传什么参数、看到结果后继续改还是结束，由模型输出决定，
  代码里没有“超时就必须压缩”这样的硬分支。
- 7 个工具：`search_materials`、`submit_script`、`estimate_duration`、`check_rules`、
  `finalize_script`、`request_clarification`、`report_unfixable`。
- 上限：12 步 / 10 次工具调用 / 2 轮改稿 / 3 次检索改写，任一触顶返回明确未完成状态。

题目参考实例已被精确复刻（[examples/trace/06_reference_p01.md](examples/trace/06_reference_p01.md)）：
P01 仅含“支持字幕”一条事实，30 秒目标 → 首稿估算 **38.0 秒** → 模型压缩后复检 **27.4 秒**
→ 终稿保留 5 个分镜、仅引用 `P01-F1`。

## 5. 验收点对照

| 验收点 | 输入 | 实际状态 | 证据 |
| --- | --- | --- | --- |
| 正常生成 | `examples/inputs/01_normal.json` | completed，28.0s | `outputs/01_normal.md`、`trace/01_normal.md` |
| 缺关键信息 | `02_missing_info.json` | needs_input，仅 1 次工具调用 | `outputs/02_missing_info.json` |
| 无素材 | `03_no_material.json` | needs_input，改写查询 3 次后请求补充 | `trace/03_no_material.md` |
| 工具失败 | `04_tool_failure.json` | 失败被观察到 → 换参重试 → completed 28.0s | `trace/04_tool_failure.md` |
| 超时长修正 | `05_over_duration.json` | 37.7s → 27.2s，两次估算后可交付 | `trace/05_over_duration.md` |
| 参考实例 P01 | `06_reference_p01.json` | 38.0s → 27.4s，仅引用 P01-F1 | `outputs/06_reference_p01.md` |
| 改变时长对照 | `07_tight_duration.json` | 同一主题压到 18s：21.7s → 18.3s，比 01 多一轮改稿 | `trace/07_tight_duration.md` |
| 达到上限 | `tests/test_limits.py` | incomplete_limit_reached | `examples/test-report.txt` |
| 防编造 | `tests/test_no_fabrication.py` | 无依据断言/百分比数据被拦截 | 同上 |

**真实模型实测**（DeepSeek，产物带 `_llm` 后缀，未覆盖 Mock 结果）：

| 用例 | 状态 | 时长 | 与 Mock 的差异 |
| --- | --- | --- | --- |
| 01_normal | completed | 23.7s | 模型自己控到 23.7s，一次通过 |
| 02_missing_info | needs_input | — | 先检索一次再反问（Mock 是直接问） |
| 03_no_material | needs_input | — | 同样改写查询后请求补充 |
| 04_tool_failure | completed | 25.6s | 工具失败后换查询重试 |
| 05_over_duration | completed | 29.0s | 初稿即达标，走「规则违规→改稿→复检」 |
| 06_reference_p01 | completed | 24.2s | 仍只引用 P01-F1，无编造参数 |

复现：`python scripts/run_examples.py --mode llm`（需自备 key，详见第 2 节）。

## 6. 已知限制

1. **时序是估算**：按“字数 ÷ 平台语速 + 每镜留白”计算，未含停顿、画面节奏，约 ±10% 偏差，
   已在产出 `risks` 中声明。
2. **Mock 只验证流程**：离线模式用人工策略表模拟决策，证明的是工具契约、护栏与 trace 可复现，
   不证明真实模型的决策质量。真实模型模式需要自备 OpenAI 兼容端点。
3. **检索是关键词匹配**：中文 2-gram 打分，无语义泛化；同义改写依赖模型或人工。
4. **事实校验是启发式**：断言词 + 数值回查原文，能拦住“编造参数/无依据效果数据”，
   但不能保证语义层面的完全一致，仍需人工终审.

各文档入口：[需求](docs/01-requirements.md) ·
[方案与取舍](docs/02-solution.md) ·
[评测与限制](docs/03-evaluation.md) ·
[AI 使用说明](docs/04-ai-usage.md) ·
[复盘](docs/05-retrospective.md)
