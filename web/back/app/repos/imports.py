"""repos/imports.py —— 导入批次的读写。

批次表只有六百来个字节一行，但它承担三个"事后必须有据可查"的职责：
幂等（同一个包不重复导）、撤销（按批次整体软删）、报告（哪条数据为什么没进来）。
所以这里的函数都写得比较直白，一眼能看出查的是哪个键。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.ids import new_public_id
from app.models import ImportBatch


def create(
    db: Session,
    *,
    device_id: int,
    fingerprint: str,
    source: str,
    mode: str,
    package_version: int,
) -> ImportBatch:
    """建一条"导入中"的批次。计数先留 0，导完由 `finish` 一次写回。"""
    batch = ImportBatch(
        public_id=new_public_id(),
        device_id=device_id,
        fingerprint=fingerprint,
        source=source,
        mode=mode,
        package_version=package_version,
        card_count=0,
        conversation_count=0,
        message_count=0,
        image_count=0,
        skipped_count=0,
        report=None,
        created_at=utcnow(),
        revoked_at=None,
    )
    db.add(batch)
    db.flush()
    return batch


def find_by_fingerprint(db: Session, device_id: int, fingerprint: str) -> ImportBatch | None:
    """同一个包再来一次 —— 这就是幂等判据。"""
    return db.scalar(
        select(ImportBatch).where(
            ImportBatch.device_id == device_id,
            ImportBatch.fingerprint == fingerprint,
        )
    )


def find_owned(db: Session, public_id: str, device_id: int) -> ImportBatch | None:
    """按 public_id 找自己的批次。别人的批次一律当不存在（防遍历）。"""
    return db.scalar(
        select(ImportBatch).where(
            ImportBatch.public_id == public_id,
            ImportBatch.device_id == device_id,
        )
    )


def list_for_device(db: Session, device_id: int, limit: int = 50) -> list[ImportBatch]:
    """这台设备的导入历史，最近的在前。"""
    stmt = (
        select(ImportBatch)
        .where(ImportBatch.device_id == device_id)
        .order_by(ImportBatch.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def finish(
    db: Session,
    batch: ImportBatch,
    *,
    counts: dict[str, int],
    report: dict[str, Any],
) -> None:
    """导入完成后把计数与明细写回批次。"""
    batch.card_count = counts.get("cards", 0)
    batch.conversation_count = counts.get("conversations", 0)
    batch.message_count = counts.get("messages", 0)
    batch.image_count = counts.get("images", 0)
    batch.skipped_count = counts.get("skipped", 0)
    batch.report = report
    db.flush()


def revoke(db: Session, batch: ImportBatch, when: datetime | None = None) -> None:
    """标记批次已撤销。**会话的软删由调用方做**（这里只认识批次表）。"""
    batch.revoked_at = when or utcnow()
    db.flush()


__all__ = [
    "create",
    "find_by_fingerprint",
    "find_owned",
    "finish",
    "list_for_device",
    "revoke",
]
