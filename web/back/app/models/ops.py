"""models/ops.py —— 运维四张表：用量、上游密钥、配置、后台任务。

第一版只有 usage_events 会被写（每次生成写一行）；另外三张先建好，
等做 key 亲和（第 4 刀）和摘要（第 6 刀）时直接用，免得那时候再改表。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Blob512, SmallFlag, UInt64, UtcDateTime


class UsageEvent(Base):
    """用量记账。一个请求一行；服务端记，客户端重装也不丢。"""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_device_time", "device_id", "created_at"),
        Index("ix_usage_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    device_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    user_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    api_key_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    # 上游前缀缓存命中量。粘性绑定到底有没有生效，只看这一个数。
    cached_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    first_token_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    total_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        ENUM("ok", "error", "timeout"), nullable=False, default="ok"
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ApiKey(Base):
    """上游密钥。密文进库，明文只在内存里出现。"""

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_keys_enabled", "enabled", "cooldown_until"),)

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    key_enc: Mapped[bytes] = mapped_column(Blob512, nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rpm: Mapped[int] = mapped_column(nullable=False, default=10)
    max_concurrent: Mapped[int] = mapped_column(nullable=False, default=1)
    enabled: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=1)
    cooldown_until: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    fail_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class ConfigEntry(Base):
    """开关与阈值。改完不用重启（每次读库，量很小）。"""

    __tablename__ = "config"

    k: Mapped[str] = mapped_column(String(64), primary_key=True)
    v: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Job(Base):
    """后台任务。表 + 进程内轮询就够，不上 Celery。"""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_state_time", "state", "run_after"),)

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(
        ENUM("pending", "running", "done", "failed"), nullable=False, default="pending"
    )
    tries: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = ["ApiKey", "ConfigEntry", "Job", "UsageEvent"]
