"""models/memory.py —— 干员记忆 + 发言权重（跨会话的那一半状态）。

三张表其实是一件事的三个面：

  * `character_memories`  **她记得你什么**（人情账，跨会话）
  * `memory_settings`     **你要不要她记得**（玩家级默认开关）
  * `character_weights`   **群里她多爱开口**（玩家自定义的发言权重）

为什么记忆挂在 `owner_key` 上而不是 `conversation_id` 上：
  这是整个功能的意义 —— 换个对话她还得认识你。归属口径和会话一致：
  `u:<user_id>`（登录）或 `d:<device_id>`（访客）。

为什么删除是 `status='deleted'` 而不是 DELETE：
  指纹留着，下次提炼才能认出"这条用户删过"，否则删完下一轮又冒出来。
  "让我忘掉"必须真的忘掉，这是这个功能敢把账本摊开给用户看的前提。
"""

from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.dialects.mysql import CHAR, ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SmallFlag, UInt64, UtcDateTime


class CharacterMemory(Base):
    """一条人情账。一条只记一件事，超过 40 字说明该拆。"""

    __tablename__ = "character_memories"
    __table_args__ = (
        UniqueConstraint(
            "owner_key", "character_id", "fingerprint", name="uk_mem_owner_char_fp"
        ),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    owner_key: Mapped[str] = mapped_column(String(32), nullable=False)
    character_id: Mapped[int] = mapped_column(UInt64, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    fingerprint: Mapped[str] = mapped_column(CHAR(16), nullable=False)
    status: Mapped[str] = mapped_column(
        ENUM("active", "superseded", "deleted"), nullable=False, default="active"
    )
    supersedes_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    source_conversation_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    pinned: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class MemorySettings(Base):
    """玩家级默认开关。没有行 = 默认 on。"""

    __tablename__ = "memory_settings"

    owner_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(ENUM("on", "off"), nullable=False, default="on")
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class CharacterWeight(Base):
    """玩家对某个干员的发言权重覆盖。没行就用 `characters.active_weight`。"""

    __tablename__ = "character_weights"

    owner_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    character_id: Mapped[int] = mapped_column(UInt64, primary_key=True)
    active: Mapped[int] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
