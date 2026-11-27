"""repos/messages.py —— 消息表的所有 SQL。

seq 是怎么分配的（这是本文件最重要的一段）：

    conversation.next_seq += 1
    seq = conversation.next_seq

前提是**调用方已经用 `conversations.lock_owned()` 锁住了这个会话行**（SELECT ... FOR UPDATE）。
有那把行锁，同一会话的并发写就是串行的，seq 不会撞。

绝不写 `SELECT MAX(seq) + 1`：两个请求同时进来会算出同一个数，
然后其中一个撞唯一键失败 —— 这是最典型的"偶发 500"。
"""

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import Conversation, Message


def insert(
    db: Session,
    *,
    conversation: Conversation,
    client_msg_id: str,
    side: str,
    text: str,
    speaker: str = "",
    attachment_id: int | None = None,
    origin: str = "v1",
    mood: str = "",
    tokens: int = 0,
    reasoning: str = "",
) -> Message:
    """往会话里追加一条消息，自动分配 seq。

    两种调用者：
      * 用户消息：client_msg_id 由客户端给（幂等键）
      * 角色消息：client_msg_id 用 `a:{seq}` —— 绝不能留空，
        否则第二条角色消息会和第一条撞 `(conversation_id, '')` 唯一键（原型上踩过）
    """
    conversation.next_seq += 1
    message = Message(
        conversation_id=conversation.id,
        seq=conversation.next_seq,
        client_msg_id=client_msg_id,
        side=side,
        speaker=speaker,
        text=text,
        reasoning=reasoning or None,
        attachment_id=attachment_id,
        visible_scope="group",
        mood=mood,
        tokens=tokens,
        origin=origin,
        created_at=utcnow(),
    )
    db.add(message)
    db.flush()
    return message


def find_by_client_msg_id(
    db: Session, conversation_id: int, client_msg_id: str, *, for_update: bool = False
) -> Message | None:
    """按幂等键找已存在的消息。找到就说明这是重发，直接把原来的 seq 还回去。

    `for_update=True` 用在"已经拿到会话行锁"之后的那次权威判断：
    MySQL 默认 REPEATABLE READ，普通 SELECT 读的是事务开始时的旧快照，
    看不到另一个请求刚刚提交的同一 client_msg_id，于是两个请求都以为自己是第一个。
    加锁读（current read）能看见最新提交的行。
    """
    stmt = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.client_msg_id == client_msg_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def last_user_seq(db: Session, conversation_id: int) -> int:
    """最后一条玩家消息的 seq（没有就是 0）。

    群聊靠它划"这一轮"的界线：seq 比它大的 AI 发言 = 本轮已经说过的。
    """
    value = db.scalar(
        select(func.max(Message.seq)).where(
            Message.conversation_id == conversation_id, Message.side == "user"
        )
    )
    return int(value or 0)


def last_assistant(db: Session, conversation_id: int) -> Message | None:
    """这个会话里最近一条角色发言。「继续说」要接着它写。"""
    return db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.side == "assistant")
        .order_by(Message.seq.desc())
        .limit(1)
    )


def list_after_seq(
    db: Session, conversation_id: int, after_seq: int, limit: int
) -> list[Message]:
    """增量拉取：只要 seq 比游标大的消息。客户端断线重连就靠这个补。"""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.seq > after_seq)
        .order_by(Message.seq)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def list_before_seq(
    db: Session, conversation_id: int, before_seq: int, limit: int
) -> list[Message]:
    """取 seq 小于游标的最近 limit 条消息，**按时间正序**返回。

    出话时用它拼历史：当前这条用户消息已经落库了（seq 就是游标），
    所以历史 = 它之前的所有消息。返回正序，因为拼提示词时是从旧到新。

    **跳过 side='system' 的行**：那是「新话题」之类的界面提示（导入老数据包时
    会带进来），屏上要显示，但对模型来说是噪声 —— 喂进去它会当成玩家说的话回。
    所以"屏上能看到的"和"喂给模型的"在这里就分开了。
    """
    stmt = (
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.seq < before_seq,
            Message.side != "system",
        )
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    rows = list(db.scalars(stmt).all())
    rows.reverse()
    return rows


def last_user(db: Session, conversation_id: int) -> Message | None:
    """最后一条玩家消息。「重新生成」拿它当锚点（重写这一轮的回话）。"""
    return db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.side == "user")
        .order_by(Message.seq.desc())
        .limit(1)
    )


def find_last_user_by_text(db: Session, conversation_id: int, text: str) -> Message | None:
    """按正文找最近一条玩家消息（重新生成更早的某一轮时用）。

    正文完全相等才算命中：客户端没有 seq 可传，只能靠这句原文对号入座。
    """
    return db.scalar(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.side == "user",
            Message.text == text,
        )
        .order_by(Message.seq.desc())
        .limit(1)
    )


def delete_after_seq(db: Session, conversation_id: int, after_seq: int) -> int:
    """删掉 seq 比游标大的消息，返回删了几条。

    只给「重新生成」用：先把这一轮的旧回复清掉，新回复才能落回原来的位置
    （否则会话里会同时留着两份回话，历史一发给模型就成了"角色说了两遍"）。

    调用方**必须**已经持有会话行锁，并把 `conversation.next_seq` 退回 after_seq；
    不然新回复的 seq 会接着往下跳，"回复紧跟在提问后面"这个约定就断了。
    """
    result = db.execute(
        delete(Message).where(
            Message.conversation_id == conversation_id, Message.seq > after_seq
        )
    )
    return int(result.rowcount or 0)
