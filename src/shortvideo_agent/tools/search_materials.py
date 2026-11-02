"""素材检索工具。

只用纯 Python 的中文 2-gram 重叠打分，不引入向量库：
- 无外部依赖、可离线、结果稳定可复现；
- 便于精确构造“检索无结果”场景（验收点之一）；
- 代价是没有语义泛化能力，同义词需由模型改写 query 或人工维护。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .. import config
from ..models import MaterialHit, ToolResult
from .registry import register

SCORE_THRESHOLD = 0.15
_HAS_FACT = re.compile(r"^-\s*\[(?P<mid>[A-Z0-9\-]+)\]\s*(?P<text>.+)$")
_STRIP = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()《》\-—…·]+")


@lru_cache(maxsize=1)
def load_corpus() -> list[dict]:
    """解析 data/products/*.md，得到一条条可引用的事实。"""
    corpus: list[dict] = []
    for path in sorted(Path(config.PRODUCT_DIR).glob("*.md")):
        product_id = path.stem
        product_name = product_id
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("# "):
                product_name = line[2:].strip()
                continue
            m = _HAS_FACT.match(line)
            if m:
                corpus.append({
                    "material_id": m.group("mid"),
                    "product_id": product_id,
                    "product_name": product_name,
                    "snippet": m.group("text").strip(),
                    "source_file": f"data/products/{path.name}",
                })
    return corpus


def _grams(text: str) -> set[str]:
    clean = _STRIP.sub("", text.lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def score(query: str, snippet: str, product_name: str = "") -> float:
    qg, sg = _grams(query), _grams(f"{snippet}{product_name}")
    if not qg or not sg:
        return 0.0
    inter = qg & sg
    return 0.7 * len(inter) / len(qg) + 0.3 * len(inter) / len(sg)


def search(query: str, top_k: int = 6, product_ids: list[str] | None = None) -> list[MaterialHit]:
    hits: list[MaterialHit] = []
    for item in load_corpus():
        if product_ids and item["product_id"] not in product_ids:
            continue
        s = score(query, item["snippet"], item["product_name"])
        if s >= SCORE_THRESHOLD:
            hits.append(MaterialHit(score=round(s, 3), **item))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


@register(
    name="search_materials",
    description=(
        "在产品素材库中检索可引用的事实。返回 material_id 与原文片段。"
        "如果返回 0 条，说明该查询在库中没有依据，可以改写 query 后重试，或请求用户补充资料。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词，建议包含产品品类与功能点"},
            "top_k": {"type": "integer", "description": "返回条数，默认 6", "default": 6},
        },
        "required": ["query"],
    },
)
def search_materials_handler(state: dict, args: dict) -> ToolResult:
    query = (args.get("query") or "").strip()
    attempt = int(state.get("search_attempts") or 0)
    if not query:
        return ToolResult(ok=False, tool="search_materials", summary="query 不能为空。", error="empty_query")

    product_ids = state.get("brief", {}).get("product_ids") or []
    hits = search(query, int(args.get("top_k") or 6), product_ids)

    patch = {
        "search_attempts": attempt + 1,
        "search_queries": list(state.get("search_queries") or []) + [query],
    }
    if hits:
        existing = {m["material_id"] for m in (state.get("materials") or [])}
        merged = list(state.get("materials") or []) + [
            h.model_dump() for h in hits if h.material_id not in existing
        ]
        patch["materials"] = merged
        patch["phase"] = "draft"
        return ToolResult(
            ok=True,
            tool="search_materials",
            summary=(
                f"命中 {len(hits)} 条素材：" + "; ".join(
                    f"{h.material_id}={h.snippet}" for h in hits
                ) + "。可以据此撰写脚本，引用时只能使用这些 material_id。"
            ),
            data={"query": query, "hits": [h.model_dump() for h in hits], "attempt": attempt + 1},
            state_patch=patch,
        )

    patch["phase"] = "research"
    remain = max(0, config.MAX_SEARCH_ATTEMPTS - (attempt + 1))
    return ToolResult(
        ok=True,
        tool="search_materials",
        summary=(
            f"检索 “{query}” 命中 0 条。素材库中没有该主题的依据"
            f"（剩余改写机会 {remain} 次）。可改写查询角度后重试；若仍无结果，应请求用户补充素材。"
        ),
        data={"query": query, "hits": [], "attempt": attempt + 1, "remaining_attempts": remain},
        state_patch=patch,
    )
