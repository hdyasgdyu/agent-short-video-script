"""CLI 层测试：参数模式、产物落盘、check 子命令的通过/拦截。"""

from __future__ import annotations

import json

from shortvideo_agent.cli import main


def _run(tmp_path, *extra: str) -> int:
    return main([
        "run", "--mode", "mock",
        "--out", str(tmp_path / "out"),
        "--trace-dir", str(tmp_path / "trace"),
        *extra,
    ])


# ---------------------------------------------------------------- run
def test_run_from_args_writes_all_artifacts(tmp_path, capsys):
    rc = _run(tmp_path, "--topic", "露营投影仪怎么选", "--seconds", "30", "--products", "P02")
    assert rc == 0

    outputs = sorted((tmp_path / "out").glob("adhoc_*.json"))
    assert len(outputs) == 1
    data = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["mode"] == "mock"
    assert data["llm_info"]
    assert data["fact_map"]

    assert list((tmp_path / "out").glob("adhoc_*.md"))
    assert list((tmp_path / "trace").glob("adhoc_*.jsonl"))
    assert list((tmp_path / "trace").glob("adhoc_*.md"))


def test_run_without_topic_returns_2(tmp_path, capsys):
    rc = main(["run", "--seconds", "30", "--out", str(tmp_path), "--trace-dir", str(tmp_path)])
    assert rc == 2
    assert "至少提供 --topic" in capsys.readouterr().out


def test_run_rejects_unknown_platform(tmp_path, capsys):
    rc = _run(tmp_path, "--topic", "露营投影仪", "--seconds", "30", "--platform", "tiktok")
    assert rc == 2
    out = capsys.readouterr().out
    assert "未知平台" in out
    assert "douyin" in out


def test_platforms_command_lists_config(capsys):
    rc = main(["platforms"])
    assert rc == 0
    out = capsys.readouterr().out
    for key in ("douyin", "shipinhao", "bilibili", "xiaohongshu"):
        assert key in out
    assert "无需改代码" in out


def test_run_missing_input_file_returns_2(tmp_path, capsys):
    rc = _run(tmp_path, "--input", str(tmp_path / "nope.json"))
    assert rc == 2
    assert "找不到输入文件" in capsys.readouterr().out


# ---------------------------------------------------------------- check
def _bad_script() -> dict:
    """含“效率提升 80%”这类无依据数据，用于验证 check 会拦截。"""
    return {
        "script": {
            "title": "测试标题",
            "platform": "douyin",
            "target_seconds": 30,
            "shots": [
                {"shot_id": 1, "visual": "画面", "narration": "这款产品效率提升 80%。", "fact_refs": []},
                {"shot_id": 2, "visual": "画面", "narration": "日常用起来挺顺手。", "fact_refs": []},
                {"shot_id": 3, "visual": "画面", "narration": "这点很关键。", "fact_refs": []},
            ],
            "disclaimer": "本视频所述功能以实际产品为准。",
        }
    }


def test_check_blocks_fabricated_data(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(_bad_script(), ensure_ascii=False), encoding="utf-8")

    rc = main(["check", "-f", str(path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "PERCENT_CLAIM_UNSOURCED" in out


def test_check_passes_on_generated_script(tmp_path, capsys):
    assert _run(tmp_path, "--topic", "露营投影仪怎么选", "--seconds", "30", "--products", "P02") == 0
    generated = sorted((tmp_path / "out").glob("adhoc_*.json"))[0]

    rc = main(["check", "-f", str(generated)])
    assert rc == 0
    assert "通过" in capsys.readouterr().out
