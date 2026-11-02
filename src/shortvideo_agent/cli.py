"""命令行入口。

- 不带子命令 / `chat`：进入交互式会话（推荐日常使用）；
- `run`：跑一个用例（读 JSON 或直接传参数），产出脚本稿、结果 JSON 与 trace；
- `check`：对已生成（或人工编辑过）的脚本重新做时长估算与规则检查。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, config
from .agent import run_case
from .llm import LLMError, make_client
from .models import Script
from .report import persist_run
from .tools.duration import estimate
from .tools.rules import check_script
from .tools.search_materials import load_corpus


def _ensure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass


def _validate_platform(case: dict) -> bool:
    """平台由 data/platform_profiles.json 驱动；写错就直接拦住，不静默套用抖音语速。"""
    platform = (case.get("brief") or {}).get("platform")
    if not platform:
        return True  # 缺失交给 Agent 反问
    profiles = config.load_platform_profiles()
    if platform in profiles:
        return True
    print(f"[错误] 未知平台 `{platform}`，可用：{', '.join(sorted(profiles))}")
    print("       新增平台：在 data/platform_profiles.json 添加一项即可，代码无需改动。")
    return False


def _case_from_args(args: argparse.Namespace) -> dict | None:
    """两种输入方式：-i 读 JSON 文件；或直接给 --topic 等参数临时构造一个用例。"""
    if args.input:
        path = Path(args.input)
        if not path.exists():
            print(f"[错误] 找不到输入文件：{path}")
            return None
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("case_id", path.stem)
        if not _validate_platform(case):
            return None
        return case

    if not args.topic:
        print("[错误] 需要提供 -i 输入文件，或至少提供 --topic。")
        print("示例：python -m shortvideo_agent run --topic \"露营投影仪怎么选\" --seconds 30 --mode llm")
        return None

    brief: dict = {"topic": args.topic}
    if args.platform:
        brief["platform"] = args.platform
    if args.seconds:
        brief["target_seconds"] = args.seconds
    if args.audience:
        brief["audience"] = args.audience
    if args.style:
        brief["style"] = args.style
    if args.products:
        brief["product_ids"] = [p.strip() for p in args.products.split(",") if p.strip()]

    case = {
        "case_id": args.case_id or f"adhoc_{datetime.now():%m%d_%H%M%S}",
        "brief": brief,
    }
    if not _validate_platform(case):
        return None
    return case


def cmd_run(args: argparse.Namespace) -> int:
    case = _case_from_args(args)
    if case is None:
        return 2

    try:
        client = make_client(args.mode)
    except LLMError as exc:
        print(f"[错误] {exc}")
        return 2

    result, trace, _ = run_case(case, client)

    # 临时用例的产物写到 ./outputs，不污染 examples/ 下的归档
    if args.out:
        out_dir = Path(args.out)
    else:
        out_dir = config.DEFAULT_OUTPUT_DIR if args.input else config.ROOT / "outputs"
    if args.trace_dir:
        trace_dir = Path(args.trace_dir)
    else:
        trace_dir = (config.ROOT / "examples" / "trace") if args.input else out_dir / "trace"

    paths = persist_run(result, trace, out_dir, trace_dir)
    md_path, json_path = paths["md"], paths["json"]
    trace_path = paths["trace_jsonl"]

    print("=" * 68)
    print(f"用例：{result.case_id}    模式：{result.mode}")
    print(f"模型：{result.llm_info}")
    print(f"状态：{result.status.value}")
    print(f"预计时长：{result.estimated_total_seconds} 秒 / 目标 {result.target_seconds} 秒")
    print(f"步数：{result.steps}    工具调用：{result.tool_calls}")
    if result.script:
        print(f"分镜数：{len(result.script.shots)}    引用素材：{sorted(result.fact_map)}")
    for m in result.messages:
        print(f"  - {m}")
    print("-" * 68)
    print(f"脚本稿：{md_path}")
    print(f"结果 JSON：{json_path}")
    print(f"Trace：{trace_path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"[错误] 找不到文件：{path}")
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("script") or data
    try:
        script = Script(**raw)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 脚本结构不合法：{exc}")
        return 2

    try:
        duration = estimate(script.shots, script.platform, script.target_seconds)
    except ValueError as exc:
        print(f"[错误] {exc}")
        return 2
    rules = config.load_brand_rules()
    report = check_script(script, load_corpus(), rules, duration)

    print("=" * 68)
    print(f"脚本：{script.title}")
    print(f"估算总时长：{duration.total_seconds}s / 目标 {duration.target_seconds}s"
          f"（超差 {duration.over_by}s，{'容差内' if duration.within_tolerance else '超限'}）")
    print(f"分镜：{len(script.shots)}    引用素材：{report.cited_materials}")
    print(f"规则检查：{'通过' if report.ok else '未通过'}")
    for v in report.violations:
        loc = f" @{v.locator}" if v.locator else ""
        print(f"  - [{v.severity}/{v.code}]{loc} {v.message}")
    print("=" * 68)
    return 0 if (report.ok and duration.within_tolerance) else 1


def cmd_chat(args: argparse.Namespace) -> int:
    from .repl import run_repl

    return run_repl(mode=args.mode, out=args.out)


def cmd_platforms(args: argparse.Namespace) -> int:
    """列出可用平台。平台清单完全由配置文件驱动，新增平台不需要改代码。"""
    profiles = config.load_platform_profiles()
    print("=" * 66)
    print(f"{'标识':<12}{'名称':<10}{'语速(字/秒)':>12}{'时长范围(秒)':>14}{'标题上限':>10}")
    print("-" * 66)
    for key in sorted(profiles):
        item = profiles[key]
        lo, hi = (item.get("target_seconds_range") or ["-", "-"])[:2]
        print(
            f"{key:<12}{str(item.get('name', '')):<10}"
            f"{item.get('chars_per_second', '-'):>12}{f'{lo}-{hi}':>14}"
            f"{item.get('title_max_chars', '-'):>10}"
        )
    print("=" * 66)
    print("新增平台：在 data/platform_profiles.json 增加一项")
    print('  {"kuaishou": {"name": "快手", "chars_per_second": 5.3,')
    print('                "target_seconds_range": [15, 180], "title_max_chars": 20}}')
    print("保存后即可用于 --platform kuaishou，无需改代码。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shortvideo-agent",
        description="短视频脚本 Agent：检索素材 → 生成脚本 → 时长/规则校验 → 动态修正",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    chat_p = sub.add_parser("chat", help="进入交互式会话（不带子命令时默认进入）")
    chat_p.add_argument("--mode", choices=["auto", "mock", "llm"], default="auto",
                        help="auto=优先真实模型，配置不可用时自动降级 mock")
    chat_p.add_argument("--out", default=None, help="产物目录，默认 ./outputs")
    chat_p.set_defaults(func=cmd_chat)

    platforms_p = sub.add_parser("platforms", help="列出已配置的平台与语速口径")
    platforms_p.set_defaults(func=cmd_platforms)

    run_p = sub.add_parser("run", help="运行一个用例（读 JSON 或直接传参数）")
    run_p.add_argument("-i", "--input", help="输入用例 JSON 路径")
    run_p.add_argument("--topic", help="主题（不提供 -i 时使用）")
    run_p.add_argument("--platform", default="douyin",
                       help="发布平台标识（由 data/platform_profiles.json 驱动），默认 douyin")
    run_p.add_argument("--seconds", type=int, help="目标时长（秒）")
    run_p.add_argument("--audience", help="目标受众")
    run_p.add_argument("--style", help="内容风格")
    run_p.add_argument("--products", help="限定素材产品，逗号分隔，如 P02,P03")
    run_p.add_argument("--case-id", dest="case_id", help="自定义用例名（决定输出文件名）")
    run_p.add_argument("--mode", choices=["mock", "llm"], default="mock",
                       help="mock=离线策略表（默认）；llm=OpenAI 兼容真实模型")
    run_p.add_argument("--out", default=None,
                       help="输出目录（默认：-i 模式写 examples/outputs，参数模式写 outputs/）")
    run_p.add_argument("--trace-dir", default=None, help="trace 目录")
    run_p.set_defaults(func=cmd_run)

    check_p = sub.add_parser("check", help="对已有脚本重新估算时长并做规则检查")
    check_p.add_argument("-f", "--file", required=True, help="结果 JSON 或脚本 JSON 路径")
    check_p.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8()
    config.load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        from .repl import run_repl

        return run_repl()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
