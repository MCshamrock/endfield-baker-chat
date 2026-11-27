"""models/imports.py —— 导入批次（老客户端/网页导出的数据包导回来的那一次）。

一行 = 一个包被导入一次。它同时承担三个角色：
  * 幂等键：`(device_id, fingerprint)` 唯一 —— 同一个包重复上传不会再建一遍会话
  * 撤销凭据：`conversations.import_batch_id` 指回这里，撤销时按批次整体软删
  * 事故报告：未收录的角色、丢掉的图、被丢弃的上下文都记在 `report` 里
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, FixedChar32, UInt64, UtcDateTime


class ImportBatch(Base):
    """一次导入。`report` 是给人看/给程序判断的明细，结构见 imports/apply.py。"""

    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_batch_public_id"),
        # 幂等键：同一台设备、同一个包，只导一次
        UniqueConstraint("device_id", "fingerprint", name="uk_batch_device_fp"),
        Index("ix_batch_device", "device_id", "id"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(FixedChar32, nullable=False)
    device_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("devices.id", name="fk_batch_device"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(ENUM("zip", "json"), nullable=False)
    mode: Mapped[str] = mapped_column(
        ENUM("merge", "replace"), nullable=False, default="merge"
    )
    package_version: Mapped[int] = mapped_column(nullable=False, default=0)
    card_count: Mapped[int] = mapped_column(nullable=False, default=0)
    conversation_count: Mapped[int] = mapped_column(nullable=False, default=0)
    message_count: Mapped[int] = mapped_column(nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(nullable=False, default=0)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


__all__ = ["ImportBatch"]
