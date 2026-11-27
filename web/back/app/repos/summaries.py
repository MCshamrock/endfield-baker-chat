"""repos/summaries.py —— 摘要表的读写。

一张会话同时只有一条 `active=1` 的摘要。版本号只增不减，旧版本留着（写坏了能回退）。

`conversations.summary_head` 是同一段文本的**副本**，存在的理由只有一个：
拼提示词时不用再解析一遍 JSON。两处必须一起更新（都收在本文件里做）。
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import Conversation, Summary


def get_active(db: Session, conversation_id: int) -> Summary | None:
    """取当前生效的摘要。没有就是还没压缩过。"""
    return db.scalar(
        select(Summary)
        .where(Summary.conversation_id == conversation_id, Summary.active == 1)
        .order_by(Summary.ver.desc())
        .limit(1)
    )


def create(
    db: Session,
    *,
    conversation: Conversation,
    summary_text: str,
    covered_to_seq: int,
) -> Summary:
    """写一版新摘要，并把上一版置为 inactive。"""
    current = get_active(db, conversation.id)
    next_ver = (current.ver + 1) if current is not None else 1

    if current is not None:
        db.execute(
            update(Summary).where(Summary.id == current.id).values(active=0)
        )

    body: dict[str, Any] = {"summary": summary_text}
    row = Summary(
        conversation_id=conversation.id,
        ver=next_ver,
        body=body,
        covered_to_seq=covered_to_seq,
        active=1,
        created_at=utcnow(),
    )
    db.add(row)
    db.flush()

    conversation.summary_head = summary_text
    db.flush()
    return row


__all__ = ["create", "get_active"]
