"""models/worldbook.py —— 世界书条目（检索的数据源）。

整份装进内存做检索（147 条约 30 KB 正文），所以这里只负责"存取"，
不承担打分逻辑（打分在 `app/retrieval/`）。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, LongText, SmallFlag, UInt64, UtcDateTime


class WorldbookEntry(Base):
    """一条世界书条目。字段名对应内核里的 t/k/s/fz/p/c。"""

    __tablename__ = "worldbook"
    __table_args__ = (
        UniqueConstraint("world_slug", "source_id", name="uk_world_source"),
        Index("ix_world_kind", "world_slug", "kind", "priority"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    world_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="term")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="mid")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    semantic: Mapped[str] = mapped_column(String(600), nullable=False, default="")
    keys: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    fuzzy: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    body: Mapped[str] = mapped_column(LongText, nullable=False)
    is_constant: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = ["WorldbookEntry"]
