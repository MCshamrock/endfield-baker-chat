"""repos/worldbook.py —— 世界书条目的读取（带内存缓存）。

为什么带缓存：检索每次请求都要用整份条目，查库就会变成每轮一次 30 KB 的往返。
缓存 60 秒，和 `content.get_safety_protocol` 同一个套路 —— 改条目后最多一分钟生效。
"""

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WorldbookEntry
from app.retrieval.store import Entry

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[list[Entry], float]] = {}


def list_entries(db: Session, world_slug: str = "endfield") -> list[Entry]:
    """读一个世界的全部条目（按编号排序），带 60 秒缓存。"""
    cached = _cache.get(world_slug)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]

    rows = db.scalars(
        select(WorldbookEntry)
        .where(WorldbookEntry.world_slug == world_slug)
        .order_by(WorldbookEntry.source_id)
    ).all()
    entries = [
        Entry(
            id=row.source_id,
            title=row.title,
            kind=row.kind,
            priority=row.priority,
            keys=[str(item) for item in (row.keys or [])],
            fuzzy=[str(item) for item in (row.fuzzy or [])],
            semantic=row.semantic,
            body=row.body,
            constant=bool(row.is_constant),
        )
        for row in rows
    ]
    _cache[world_slug] = (entries, now + _CACHE_TTL_SECONDS)
    return entries


def clear_cache() -> None:
    """测试或后台改完条目后调用。"""
    _cache.clear()


__all__ = ["clear_cache", "list_entries"]
