"""repos/api_keys.py —— 上游 key 表的读写。

表里只有密文（`key_enc`），解密在 `app/keys.py` 里做，且只在进程内存里留明文。
这一层不做解密，免得"随手一查就把明文带出来了"。
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import ApiKey


def list_all(db: Session) -> list[ApiKey]:
    """所有 key（含禁用的），供池子自己筛。"""
    return list(db.scalars(select(ApiKey).order_by(ApiKey.id)).all())


def list_usable(db: Session) -> list[ApiKey]:
    """可用候选：enabled=1，且不在冷却期。"""
    now = utcnow()
    stmt = (
        select(ApiKey)
        .where(
            ApiKey.enabled == 1,
            (ApiKey.cooldown_until.is_(None)) | (ApiKey.cooldown_until <= now),
        )
        .order_by(ApiKey.id)
    )
    return list(db.scalars(stmt).all())


def upsert(
    db: Session,
    *,
    label: str,
    key_enc: bytes,
    base_url: str,
    model: str,
    rpm: int,
    enabled: bool = True,
) -> ApiKey:
    """按 label 幂等写入（导入脚本用，重跑不会产生重复 key）。"""
    existing = db.scalar(select(ApiKey).where(ApiKey.label == label))
    if existing is None:
        key = ApiKey(
            label=label,
            key_enc=key_enc,
            base_url=base_url,
            model=model,
            rpm=rpm,
            max_concurrent=1,
            enabled=1 if enabled else 0,
            fail_count=0,
            created_at=utcnow(),
        )
        db.add(key)
        db.flush()
        return key

    existing.key_enc = key_enc
    existing.base_url = base_url
    existing.model = model
    existing.rpm = rpm
    existing.enabled = 1 if enabled else 0
    db.flush()
    return existing


def mark_cooldown(db: Session, key: ApiKey, until: datetime) -> None:
    key.cooldown_until = until
    db.flush()


def mark_failure(db: Session, key: ApiKey) -> int:
    """失败计数 +1，返回新值。"""
    key.fail_count = (key.fail_count or 0) + 1
    db.flush()
    return key.fail_count


def mark_success(db: Session, key: ApiKey) -> None:
    """成功一次：失败计数清零、记最后使用时间。"""
    key.fail_count = 0
    key.last_used_at = utcnow()
    db.flush()


__all__ = [
    "list_all",
    "list_usable",
    "mark_cooldown",
    "mark_failure",
    "mark_success",
    "upsert",
]
