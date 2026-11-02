"""交互式会话（REPL）。

目标是把“跑一次命令看一次结果”变成“边说边改”：
- 自然语言直接给主题、时长、平台，不用写 JSON；
- 缺信息时 Agent 反问，下一句话补充即可继续；
- 每一步工具调用实时打印（动作 + 观察），不是跑完才吐一个结果；
- 产物照常落盘，可随时 /show 回看、/trace 查过程。

注意：这里的“自然语言解析”是轻量正则，不额外消耗模型调用；
真正的脚本生成与决策仍然走 LangGraph + 模型。
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from .agent import run_case
from .llm import LLMError, make_client
from .models import ScriptResult, Status
from .report import persist_run


# ------------------------------------------------------------------ 终端样式
def _enable_ansi() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:  # noqa: BLE001
        return False


_ANSI = _enable_ansi()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text


def _prepare_io() -> None:
    """统一输出编码；非交互输入（管道）时尽量按 UTF-8 读，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    if sys.stdin is not None and hasattr(sys.stdin, "reconfigure"):
        try:
            if not sys.stdin.isatty():
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


# ------------------------------------------------------------------ 输入解析
_PLATFORM_CACHE: dict[str, str] | None = None


def _platform_aliases() -> dict[str, str]:
    """平台别名来自 data/platform_profiles.json，新增平台后交互模式自动识别。"""
    global _PLATFORM_CACHE
    if _PLATFORM_CACHE is None:
        aliases = {"b站": "bilibili", "小破站": "bilibili"}
        for key, item in config.load_platform_profiles().items():
            aliases[key.lower()] = key
            name = str(item.get("name") or "").strip()
            if name:
                aliases[name.lower()] = key
        _PLATFORM_CACHE = aliases
    return _PLATFORM_CACHE
# 单字停用词只做整词匹配，避免把“用户”“写字”这类主题词削掉
_STOP_EXACT = {"的", "了", "做", "用", "写", "帮", "给", "吧", "呢", "啊", "个", "条"}
# 口语赘词做前后缀剥离，长的优先匹配
_STOP_AFFIX = sorted(
    [
        "帮我", "帮忙", "写一个", "写一段", "写一篇", "写个", "生成", "做一个",
        "做一条", "做个", "来一条", "来一个", "一条", "一段", "脚本", "文案",
        "短视频", "视频", "内容", "请",
    ],
    key=len,
    reverse=True,
)
_TIME_SEC = re.compile(r"(\d+)\s*秒")
_TIME_MIN = re.compile(r"(\d+)\s*分(?:钟)?")
_PRODUCT = re.compile(r"p0\d", re.IGNORECASE)
_SPLIT = re.compile(r"[\s，,。、；;：:！!？?“”‘’\"'（）()]+")


def _strip_stop(word: str) -> str:
    if word in _STOP_EXACT:
        return ""
    changed = True
    while changed and word:
        changed = False
        # “露营投影仪的”“的会议耳机”这类口语化结构，去掉领属助词
        if word.startswith("的") and len(word) > 1:
            word = word[1:]
            changed = True
        if word.endswith("的") and len(word) > 1:
            word = word[:-1]
            changed = True
        for stop in _STOP_AFFIX:
            if word == stop:
                return ""
            if len(word) > len(stop) and word.startswith(stop):
                word = word[len(stop):]
                changed = True
            if len(word) > len(stop) and word.endswith(stop):
                word = word[: -len(stop)]
                changed = True
    return "" if word in _STOP_EXACT else word


def parse_brief(text: str) -> dict[str, Any]:
    """从一句自然语言里抽 brief 字段。抽不到就不填，交给 Agent 反问。"""
    out: dict[str, Any] = {}
    lower = text.lower()

    m = _TIME_SEC.search(text)
    if m:
        out["target_seconds"] = int(m.group(1))
    else:
        m = _TIME_MIN.search(text)
        if m:
            out["target_seconds"] = int(m.group(1)) * 60

    aliases = _platform_aliases()
    for word in sorted(aliases, key=len, reverse=True):
        if word in lower:
            out["platform"] = aliases[word]
            break

    ids = _PRODUCT.findall(text)
    if ids:
        out["product_ids"] = sorted({i.upper() for i in ids})

    rest = _TIME_SEC.sub(" ", text)
    rest = _TIME_MIN.sub(" ", rest)
    for word in sorted(aliases, key=len, reverse=True):
        rest = rest.replace(word, " ")
    rest = _PRODUCT.sub(" ", rest)

    topic = "".join(t for t in (_strip_stop(p) for p in _SPLIT.split(rest)) if t)
    if len(topic) >= 2:
        out["topic"] = topic
    return out


# ------------------------------------------------------------------ 会话
@dataclass
class Session:
    mode: str = "auto"
    brief: dict[str, Any] = field(default_factory=dict)
    client: Any | None = None
    last_result: ScriptResult | None = None
    last_paths: dict[str, Path] = field(default_factory=dict)
    last_status: str | None = None

    def reset(self) -> None:
        self.brief = {}
        self.last_result = None
        self.last_paths = {}
        self.last_status = None


def _ensure_client(session: Session):
    if session.client is not None:
        return session.client
    if session.mode == "auto":
        try:
            session.client = make_client("llm")
        except LLMError as exc:
            session.client = make_client("mock")
            print(f"  {dim('模型')} {yellow(session.client.describe())}")
            print(dim(f"       未检测到模型配置，已降级为离线 Mock（{exc}）"))
            return session.client
    else:
        session.client = make_client(session.mode)

    print(f"  {dim('模型')} {cyan(session.client.describe())}")
    return session.client


def _prompt(session: Session) -> str:
    """提示符带上当前模型，避免多轮之后忘了现在用的是真模型还是 Mock。"""
    name = session.client.short_name() if session.client else session.mode
    return f"{bold('你')} {dim('[' + name + ']')} › "


# ------------------------------------------------------------------ 过程打印
def _format_args(name: str, args: dict) -> str:
    if "script" in args and isinstance(args["script"], dict):
        shots = args["script"].get("shots") or []
        chars = sum(len(s.get("narration") or "") for s in shots)
        return f"{len(shots)} 镜 / {chars} 字"
    if "query" in args:
        return f'query="{args["query"]}"'
    if "questions" in args:
        return f"{len(args['questions'])} 个问题"
    if "reason" in args and len(args) == 1:
        return ""
    return ""


def _make_printer(started: float):
    def on_step(entry: dict) -> None:
        elapsed = f"{time.time() - started:4.1f}s"
        if entry.get("actor") == "model":
            name = entry.get("tool") or "（无动作）"
            suffix = _format_args(name, entry.get("args") or {})
            print(f"  {cyan('⚙')} {name} {dim(suffix)} {dim(elapsed)}")
        else:
            summary = (entry.get("result_summary") or "").split("\n")[0]
            if len(summary) > 96:
                summary = summary[:93] + "..."
            mark = green("↳") if entry.get("ok") else red("✗")
            print(f"    {mark} {dim(summary)}")

    return on_step


def _print_result(result: ScriptResult, paths: dict[str, Path]) -> None:
    script = result.script
    if result.status in (Status.completed, Status.completed_with_warnings) and script:
        refs = ",".join(sorted(result.fact_map)) or "—"
        print()
        print(
            f"  {green('✔')} {bold(result.status.value)} · "
            f"{result.estimated_total_seconds}s / {result.target_seconds}s · "
            f"{len(script.shots)} 镜 · 素材 {refs}"
        )
        print(f"  {bold(script.title)}")
        per = result.duration.per_shot if result.duration else {}
        for shot in script.shots:
            d = per.get(str(shot.shot_id), "-")
            print(f"  {dim(str(shot.shot_id).rjust(2))} {dim(f'{d}s'.rjust(6))}  {shot.narration}")
        print()
        print(dim(f"  稿子：{paths['md']}"))
        print(dim(f"  溯源：{paths['json']}"))
        print(dim(f"  过程：{paths['trace_md']}"))
    elif result.status == Status.needs_input:
        print()
        print(f"  {yellow('?')} 还需要你补充：")
        for m in result.messages:
            print(f"    {m[2:] if m.startswith('- ') else m}")
    else:
        print()
        print(f"  {red('✗')} {bold(result.status.value)}")
        for m in result.messages:
            print(f"    {m[2:] if m.startswith('- ') else m}")
        if paths:
            print(dim(f"  过程：{paths.get('trace_md')}"))


# ------------------------------------------------------------------ 一轮对话
def handle_turn(session: Session, text: str, out_dir: Path) -> ScriptResult | None:
    delta = parse_brief(text)

    if session.brief and session.last_status == Status.needs_input.value:
        # 反问之后的补充：只覆盖识别到的字段，不把补充句当成新主题
        for key, value in delta.items():
            if key == "topic" and len(value) < 4:
                continue
            session.brief[key] = value
    else:
        session.brief.update(delta)

    # 只说了产品没给主题（如“用 P01 写 30 秒脚本”）时，用产品作为落脚点
    if not session.brief.get("topic") and session.brief.get("product_ids"):
        session.brief["topic"] = f"{session.brief['product_ids'][0]} 产品介绍"

    if not session.brief.get("topic"):
        print(yellow("  ? 先给我一个主题，例如：露营投影仪怎么选，30 秒，抖音"))
        return None

    client = _ensure_client(session)
    brief = session.brief
    summary = " | ".join(
        str(x) for x in (
            brief.get("topic"),
            brief.get("platform") or "未指定平台",
            f"{brief['target_seconds']}s" if brief.get("target_seconds") else "未指定时长",
        )
    )
    print(dim(f"  · {summary}"))

    case = {
        "case_id": f"chat_{datetime.now():%m%d_%H%M%S}",
        "brief": dict(brief),
    }
    started = time.time()
    try:
        result, trace, _ = run_case(case, client, on_step=_make_printer(started))
    except LLMError as exc:
        print(red(f"  ✗ 模型不可用：{exc}"))
        return None

    paths = persist_run(result, trace, out_dir, out_dir / "trace")
    session.last_result = result
    session.last_paths = paths
    session.last_status = result.status.value

    _print_result(result, paths)
    if result.status == Status.failed_tool_error and session.mode == "auto":
        print(yellow("  提示：模型调用失败，可用 /mode mock 切到离线模式验证流程。"))
    return result


# ------------------------------------------------------------------ 命令
HELP = """
可用命令
  /help              显示帮助
  /model             查看当前模式、模型端点与简报（同 /status）
  /mode mock|llm     切换模型（auto 会优先用真实模型）
  /set 键=值 ...      手动设置字段，如 /set target_seconds=45 platform=xiaohongshu
  /show              重新打印上一版脚本
  /trace             打印上一版的执行记录路径
  /reset             清空会话，重新开始
  /quit              退出

直接说话即可，例如：
  帮我写个露营投影仪的 30 秒抖音脚本
  时长改成 20 秒，平台换成小红书
"""


def _print_help() -> None:
    print(HELP)


def handle_command(session: Session, line: str) -> str:
    cmd, _, rest = line.partition(" ")
    cmd = cmd.strip().lower()
    rest = rest.strip()

    if cmd in ("/quit", "/exit", "/q"):
        return "quit"
    if cmd == "/help":
        _print_help()
    elif cmd in ("/model", "/status"):
        _print_status(session)
    elif cmd == "/mode":
        if rest not in ("auto", "mock", "llm"):
            print(yellow("  用法：/mode auto|mock|llm"))
        else:
            session.mode = rest
            session.client = None
            print(dim(f"  已切换模式：{rest}"))
            _ensure_client(session)
    elif cmd == "/set":
        if not rest:
            print(yellow("  用法：/set target_seconds=45 platform=xiaohongshu"))
        else:
            _apply_manual_set(session, rest)
    elif cmd == "/show":
        if session.last_result and session.last_result.script:
            _print_result(session.last_result, session.last_paths)
        else:
            print(dim("  还没有生成过脚本。"))
    elif cmd == "/trace":
        if session.last_paths:
            print(dim(f"  {session.last_paths['trace_md']}"))
            print(dim(f"  {session.last_paths['trace_jsonl']}"))
        else:
            print(dim("  还没有执行记录。"))
    elif cmd == "/reset":
        session.reset()
        print(dim("  会话已清空。"))
    else:
        print(yellow(f"  未知命令：{cmd}，输入 /help 查看可用命令"))
    return "ok"


def _print_status(session: Session) -> None:
    if session.client is None:
        _ensure_client(session)
    print(f"  模式：{session.mode}")
    print(f"  模型：{cyan(session.client.describe() if session.client else '-')}")
    print(f"  简报：{session.brief or '（空）'}")
    print(f"  上一轮：{session.last_status or '（还没跑过）'}")
    if session.last_paths:
        print(f"  产物：{session.last_paths['md']}")


def _apply_manual_set(session: Session, rest: str) -> None:
    for part in rest.split():
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key == "target_seconds":
            try:
                session.brief[key] = int(value)
            except ValueError:
                print(yellow(f"  target_seconds 需要整数，收到：{value}"))
        elif key == "product_ids":
            session.brief[key] = [v.strip().upper() for v in value.split(",") if v.strip()]
        elif key == "platform":
            profiles = config.load_platform_profiles()
            if value in profiles:
                session.brief[key] = value
            else:
                print(yellow(f"  未知平台 `{value}`，可用：{', '.join(sorted(profiles))}"))
        elif key in ("topic", "audience", "style"):
            session.brief[key] = value
        else:
            print(yellow(f"  不认识的字段：{key}"))
    print(dim(f"  当前简报：{session.brief}"))


# ------------------------------------------------------------------ 主循环
def run_repl(mode: str = "auto", out: str | Path | None = None) -> int:
    _prepare_io()
    out_dir = Path(out) if out else (config.ROOT / "outputs")
    session = Session(mode=mode)

    print()
    print(bold("  短视频脚本 Agent") + dim("  ·  交互模式"))
    _ensure_client(session)
    print(dim("  直接说需求即可，例如：帮我写个露营投影仪的 30 秒抖音脚本"))
    print(dim("  输入 /help 查看命令，/quit 退出"))
    print()

    while True:
        try:
            line = input(_prompt(session)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  再见。")
            return 0

        if not line:
            continue
        if _has_surrogate(line):
            print(yellow("  ! 输入编码异常（当前终端不是 UTF-8）。Windows 可先执行 chcp 65001 再重试。"))
            continue
        if line.startswith("/"):
            if handle_command(session, line) == "quit":
                print("  再见。")
                return 0
            continue
        try:
            handle_turn(session, line, out_dir)
        except KeyboardInterrupt:
            print(yellow("\n  已中断本轮。"))
