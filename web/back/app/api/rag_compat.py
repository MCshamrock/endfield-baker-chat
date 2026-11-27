"""api/rag_compat.py —— 老检索服务的兼容出口：`/retrieve` 交给算法检索回答。

## 为什么有这个东西

老网站和老客户端里有两处在打「RAG 检索」这个口子（2026-11-22 生产实测）：

  1. 老后端 `rag_client.py`：`POST /retrieve`，读 `data["passages"]`；另有 `/health`、`/stats`；
  2. 网页 / 安卓老版本直连 `rag.peilika.beer`：`POST /retrieve`，读 `data["results"]`。

新后端的知识本来就不靠向量了（`app/retrieval/` 是词法三档 + 标题加分），
数据和聊天里预取的是**同一份世界书**。所以把这个口子接到同一个检索上，
既不用维护第二套语料，也能把那张 GPU 上的向量服务关掉。

## 两套字段一起给

两边读的键不一样（`passages` / `results`），单元字段也不一样（`id/doc_id` 与 `rank/score`），
所以这里**一次返回两套**，谁读哪个都行 —— 比让老客户端改代码省事得多。

## 分数的口径

老服务返回的是向量余弦相似度（0~1，客户端默认 `min_score=0.4` 才收）。
算法检索的分数是"命中几个触发词 + 标题加分"（几十的量级），不能直接拿去比 0.4。
这里把本批最高分映射到 0.99、最低到 0.40 —— 保持"越相关分越高"的相对关系，
也让老客户端那个 0.4 的阈值仍然说得通。
"""

import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.deps import DbSession
from app.repos import worldbook as worldbook_repo
from app.retrieval.store import Entry, match_entries, search_entries
from app.settings import get_settings

router = APIRouter(tags=["rag-compat"])

# 世界书的 kind → 老 RAG 语料里的 category（老客户端只拿它显示，不参与判断）
CATEGORY_BY_KIND = {
    "term": "archive",
    "place": "archive",
    "rule": "archive",
    "person": "character",
    "event": "story",
}

# 一次请求最多取多少条（老客户端默认 4，给它留点余量但别让人拿去刷）
MAX_TOP_K = 20

# 同一篇最多占几个位置：老 RAG 是一块一块返回的（「四号谷地」就有两块），
# 全给同一篇会让 top_k 白瞎，所以允许两小块、再多就让位给别的条目。
MAX_PER_TITLE = 2
MAX_QUERY_CHARS = 200


class RetrieveIn(BaseModel):
    """老检索服务的请求体：`{query, top_k, min_score}`。"""

    query: str = Field(default="", max_length=MAX_QUERY_CHARS)
    top_k: int = 4
    k: int | None = None  # 有的客户端写 k，一起认
    min_score: float = 0.0


def _category(entry: Entry) -> str:
    return CATEGORY_BY_KIND.get(entry.kind, "archive")


def _doc_id(entry: Entry) -> str:
    """拼一个和原语料同形状的定位（`中枢档案/超域.md#0`）。

    世界书表里只存条目，不存原始文件路径；这里按 category/标题 还原，
    够老客户端在 trace 里看，不参与任何判断。
    """
    return f"{_category(entry)}/{entry.title}.md#0"


def _pick(entries: list[Entry], query: str, top_k: int) -> list[tuple[Entry, float]]:
    """先走关键词预取（和聊天同一条路），没命中再用模糊档兜错别字。"""
    hits = match_entries(entries, query, max_hits=max(top_k * 4, 12))
    if not hits:
        hits = [
            {
                "id": hit["id"],
                "title": hit["title"],
                "score": hit["score"],
            }
            for hit in search_entries(entries, query, "fuzzy", max_hits=max(top_k * 4, 12))["hits"]
        ]
    by_id = {entry.id: entry for entry in entries}
    picked: list[tuple[Entry, float]] = []
    per_title: dict[str, int] = {}
    for hit in hits:
        entry = by_id.get(str(hit["id"]))
        if entry is None:
            continue
        used = per_title.get(entry.title, 0)
        if used >= MAX_PER_TITLE:
            continue
        per_title[entry.title] = used + 1
        picked.append((entry, float(hit["score"])))
        if len(picked) >= top_k:
            break
    return picked


@router.post("/retrieve")
def retrieve(payload: RetrieveIn, db: DbSession) -> dict[str, object]:
    """老 RAG 的检索接口，改由算法检索回答（同时给 `passages` 和 `results` 两套字段）。"""
    started = time.perf_counter()
    query = (payload.query or "").strip()
    settings = get_settings()
    top_k = max(1, min(payload.k or payload.top_k or 4, MAX_TOP_K))

    picked: list[tuple[Entry, float]] = []
    if query and settings.worldbook_enabled:
        entries = worldbook_repo.list_entries(db, settings.worldbook_slug)
        if entries:
            picked = _pick(entries, query, top_k)

    best = max((score for _, score in picked), default=0.0) or 1.0
    results = []
    passages = []
    for rank, (entry, score) in enumerate(picked, 1):
        # 归一化：本批最高 → 0.99，最低 → 0.40（老客户端的阈值就是 0.4）
        normalized = round(0.40 + 0.59 * (score / best), 4)
        if normalized < payload.min_score:
            continue
        text = f"【{entry.title}】{entry.body}"
        results.append(
            {
                "rank": rank,
                "score": normalized,
                "title": entry.title,
                "category": _category(entry),
                "text": text,
            }
        )
        passages.append(
            {
                "id": entry.id,
                "doc_id": _doc_id(entry),
                "category": _category(entry),
                "title": entry.title,
                "text": text,
                "score": normalized,
            }
        )

    return {
        "query": query,
        "count": len(results),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "model": "worldbook-algo",
        "sources": ["algo"],
        "results": results,
        "passages": passages,
    }


@router.get("/stats")
def stats(db: DbSession) -> dict[str, object]:
    """老服务有个 `/stats`（运维看实例状态用）。这里报语料规模和检索方式。"""
    settings = get_settings()
    entries = worldbook_repo.list_entries(db, settings.worldbook_slug)
    return {
        "ok": True,
        "mode": "algo",
        "world": settings.worldbook_slug,
        "entries": len(entries),
        "titles": len({entry.title for entry in entries}),
    }


__all__ = ["router"]
