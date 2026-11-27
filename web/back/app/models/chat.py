"""models/chat.py —— 聊天五张表：会话、群成员、消息、附件、摘要。

这一组的核心是 **conversations + messages**：
  会话持有 `next_seq`，消息靠 `(conversation_id, seq)` 和 `(conversation_id, client_msg_id)`
  两个唯一键保证顺序正确、重发不重复。

群聊成员表（conversation_members）第一版只建不用，等第 8 刀做群聊时再读写。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, FixedChar32, LongText, SmallFlag, UInt64, UtcDateTime


class Conversation(Base):
    """会话。客户端只拿 public_id 当指针，历史全在服务端。"""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_conv_public_id"),
        Index("ix_conv_device_active", "device_id", "last_active_at"),
        Index("ix_conv_user", "user_id", "last_active_at"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(FixedChar32, nullable=False)
    device_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("devices.id", name="fk_conv_device"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    character_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    mode: Mapped[str] = mapped_column(ENUM("single", "group"), nullable=False, default="single")
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # next_seq 只在锁住这一行之后才 +1，绝不 MAX(seq)+1（并发会撞）
    next_seq: Mapped[int] = mapped_column(UInt64, nullable=False, default=0)
    api_key_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    summary_head: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 永久记忆：二次压缩产物（摘要再压一次，只留关系/承诺/悬念），≤1200 字
    memory: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 群聊设置：谁来说、玩家以什么身份在场
    speak_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="weighted")
    player_role: Mapped[str] = mapped_column(String(64), nullable=False, default="observer")
    # 干员记忆：这一局要不要让她记得你（inherit 跟随全局默认）
    memory_mode: Mapped[str] = mapped_column(
        ENUM("inherit", "on", "off"), nullable=False, default="inherit"
    )
    # 由哪次数据包导入创建（NULL = 用户自己建的）。撤销导入时按它整体软删
    import_batch_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class ConversationMember(Base):
    """群聊成员。单聊不写这张表。"""

    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "ord", name="uk_members_conv_ord"),)

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("conversations.id", name="fk_members_conv"), nullable=False
    )
    character_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("characters.id", name="fk_members_char"), nullable=False
    )
    ord: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(ENUM("ai", "player"), nullable=False, default="ai")


class Message(Base):
    """消息。两个唯一键 = 全部幂等保证，应用层不再自己判重。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uk_msg_conv_seq"),
        UniqueConstraint("conversation_id", "client_msg_id", name="uk_msg_conv_client"),
        Index("ix_msg_conv_id", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("conversations.id", name="fk_msg_conv"), nullable=False
    )
    seq: Mapped[int] = mapped_column(UInt64, nullable=False)
    # assistant 消息没有客户端 id，用 a:{seq} 填；留空会撞 (conversation_id, '') 唯一键
    client_msg_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(
        ENUM("user", "assistant", "system"), nullable=False
    )
    speaker: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    text: Mapped[str] = mapped_column(LongText, nullable=False)
    # 上游思考过程：只存服务端，**任何下发接口都不许带它**（项目书要求，见 docs/08）
    reasoning: Mapped[str | None] = mapped_column(LongText, nullable=True)
    attachment_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    visible_scope: Mapped[str] = mapped_column(
        ENUM("private", "group"), nullable=False, default="group"
    )
    mood: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    origin: Mapped[str] = mapped_column(ENUM("v1", "legacy"), nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Attachment(Base):
    """附件。文件按内容 sha256 只存一份，但**每条记录属于一台设备**。

    去重粒度是 `(device_id, sha256)` 而不是 `sha256`（迁移 011 改的）：
    全局唯一时，新设备传同一张图会拿到别人那条记录，取回时按归属一校验就 404。
    """

    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_att_public_id"),
        UniqueConstraint("device_id", "sha256", name="uk_att_device_sha"),
        Index("ix_att_sha", "sha256"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(FixedChar32, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 上传者设备：附件按内容去重（同一张图只存一份），但归属必须记得住
    device_id: Mapped[int | None] = mapped_column(UInt64, nullable=True)
    mime: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    bytes: Mapped[int] = mapped_column(UInt64, nullable=False, default=0)
    width: Mapped[int] = mapped_column(nullable=False, default=0)
    height: Mapped[int] = mapped_column(nullable=False, default=0)
    path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class Summary(Base):
    """摘要。结构化 JSON（memory/plot/state/timeline），版本化便于回退。"""

    __tablename__ = "summaries"
    __table_args__ = (UniqueConstraint("conversation_id", "ver", name="uk_sum_conv_ver"),)

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("conversations.id", name="fk_sum_conv"), nullable=False
    )
    ver: Mapped[int] = mapped_column(nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    covered_to_seq: Mapped[int] = mapped_column(UInt64, nullable=False)
    active: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = [
    "Attachment",
    "Conversation",
    "ConversationMember",
    "Message",
    "Summary",
]
