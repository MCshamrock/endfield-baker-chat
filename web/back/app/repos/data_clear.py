"""repos/data_clear.py —— 「清空服务器数据」这一组 SQL。

为什么单独一个文件（不塞进 conversations.py / messages.py）：
  聊天主链路的删除是**软删**（`deleted_at`，行留着方便排查"用户说记录没了"），
  而这里跟的是另一条口径 —— 用户亲手点了「删除全部」，他要的是**数据真的没了**，
  所以这里是物理 `DELETE`。两种口径混在一个文件里，以后谁改软删逻辑都会顺手
  把这里改坏（软删对"清空数据"是错的：行还在库里，换个接口就可能又露出来）。

归属口径与 `repos/conversations.py` 的 `_owner_scope` **完全一致**，一个字都没敢改：
  * 登录：按 `user_id`（该账号名下所有设备的数据都是他的）
  * 访客：按 `device_id`，并且要求 `user_id IS NULL`
    （这台设备以前登录过别人，那些记录不属于现在的访客）
记错这条就是"清了别人的数据"这种不可挽回的事故，所以两边必须同一套判定。

两个容易忽略的点：
  1. **外键顺序**：`messages` / `summaries` / `conversation_members` 都有指向
     `conversations.id` 的外键，必须先删子表，再删会话行，否则 MySQL 报 1451。
  2. **`IN` 列表分片**：一次塞几千个 id 进 `IN (...)`，MySQL 会撞
     `max_allowed_packet` / 占位符上限。这里固定 500 一片，脏活藏在一个函数里。
"""

from collections.abc import Iterator

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.orm import Session

from app.models import CharacterMemory, Conversation, ConversationMember, Message, Summary

# 一次 `IN (...)` 里最多放多少个 id（见文件头第 2 条）
_CHUNK = 500


def _chunks(values: list[int], size: int = _CHUNK) -> Iterator[list[int]]:
    """把 id 列表切成小片，供 `IN (...)` 用。"""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def owner_scope(user_id: int | None, device_id: int) -> ColumnElement[bool]:
    """"这条会话是不是我的"。

    与 `repos/conversations.py::_owner_scope` 同一套规则（那边是私有的，
    这里再写一份而不是 import 私有名，是为了让本文件单独可读；
    两边若哪天要改，必须一起改 —— 这正是单测 `test_data_clear.py` 守的东西）。
    """
    if user_id is not None:
        return Conversation.user_id == user_id
    return (Conversation.device_id == device_id) & Conversation.user_id.is_(None)


def conversation_ids(db: Session, *, user_id: int | None, device_id: int) -> list[int]:
    """归属者名下的全部会话 id。

    **不排除软删的行**：这里不是"列表"，是"清空"。用户上次单删留下的行
    也属于他的服务器数据，一起清掉才算真清空（而且这些行还占着唯一的 public_id）。
    """
    stmt = select(Conversation.id).where(owner_scope(user_id, device_id))
    return [int(value) for value in db.scalars(stmt)]


def _delete_messages(db: Session, conversation_ids: list[int]) -> int:
    """删这些会话下的全部消息（含 reasoning 等只在服务端的列）。"""
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            delete(Message)
            .where(Message.conversation_id.in_(chunk))
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def _delete_summaries(db: Session, conversation_ids: list[int]) -> int:
    """删这些会话下的全部摘要（所有版本，不只 active 那条）。"""
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            delete(Summary)
            .where(Summary.conversation_id.in_(chunk))
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def _delete_members(db: Session, conversation_ids: list[int]) -> int:
    """删群成员行（有外键指向会话，必须先走）。"""
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            delete(ConversationMember)
            .where(ConversationMember.conversation_id.in_(chunk))
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def _delete_conversations(db: Session, conversation_ids: list[int]) -> int:
    """删会话行本身（子表必须已经删完，见文件头第 1 条）。"""
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            delete(Conversation)
            .where(Conversation.id.in_(chunk))
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def _reset_next_seq(db: Session, conversation_ids: list[int]) -> int:
    """把 `next_seq` 归零。

    为什么必须做：seq 是 `next_seq += 1` 分配的。消息删光但 next_seq 留在 12，
    新消息就从 13 开始 —— 客户端按 `after_seq=0` 增量拉，看到的第一条是 13，
    界面上像"断档"，也让"这条会话的 seq 从 1 开始"这个隐含契约失效。
    """
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            update(Conversation)
            .where(Conversation.id.in_(chunk))
            .values(next_seq=0)
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def _clear_conversation_context(db: Session, conversation_ids: list[int]) -> int:
    """清掉会话上的摘要副本与永久记忆（`summary_head` / `memory`）。

    这两列是同一份内容的**副本**（拼提示词时不用再解析 JSON），摘要行删了
    副本还留着的话，下一轮出话仍会把"她已经忘记的那段"塞进提示词。
    """
    total = 0
    for chunk in _chunks(conversation_ids):
        result = db.execute(
            update(Conversation)
            .where(Conversation.id.in_(chunk))
            .values(summary_head=None, memory=None)
            .execution_options(synchronize_session=False)
        )
        total += int(result.rowcount or 0)
    return total


def delete_memories(db: Session, owner_key: str) -> int:
    """删掉这个归属者的**全部**干员记忆（物理删）。

    为什么不是聊天记忆那种"软删 + 留指纹防复活"：
      软删是为了防"删完下一轮又被提炼回来"。用户点的是「清空全部上下文」——
      连同会话历史一起清的场景里，根本没有"下一轮从旧历史里再提炼"的机会；
      而只清上下文、消息还留着的场景，留指纹的理由也不成立（他要的就是忘干净）。
      所以这里物理删：账本归零，`/v1/memories` 立刻看不到任何条目。
    """
    result = db.execute(
        delete(CharacterMemory)
        .where(CharacterMemory.owner_key == owner_key)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def clear(
    db: Session,
    *,
    user_id: int | None,
    device_id: int,
    owner_key: str,
    conversations: bool,
    messages: bool,
    context: bool,
) -> dict[str, int]:
    """按 flag 清数据，返回各表实际删掉的行数。

    **不 commit**：调用方（API 层）把这一整段包在一个事务里提交，见 `api/data.py`。
    `context` 的计数口径：`summaries` 是摘要行，`memories` 是干员记忆行。
    三条 flag 同时为真时也不会重复计数 —— 摘要上一步删过了，下一步的
    DELETE 只会返回 0（这里的计数一律用 `+=`，不是 `=`）。
    """
    counts = {"conversations": 0, "messages": 0, "summaries": 0, "memories": 0}
    ids = conversation_ids(db, user_id=user_id, device_id=device_id)

    if conversations:
        # 顺序 = 外键顺序（子表先走），别调换
        counts["messages"] += _delete_messages(db, ids)
        counts["summaries"] += _delete_summaries(db, ids)
        _delete_members(db, ids)
        # 注意：会话行删了之后，下面 context 里对会话列的 UPDATE 会更新 0 行，
        # 这是预期行为（没有会话就没有"摘要副本"可清）
        counts["conversations"] += _delete_conversations(db, ids)

    if messages:
        # 会话留着，只清消息：清完要把 next_seq 归零，否则新消息 seq 跳号
        counts["messages"] += _delete_messages(db, ids)
        if ids:
            _reset_next_seq(db, ids)

    if context:
        # 摘要（会话级） + 干员记忆（账号/设备级） + 会话上的两份副本
        counts["summaries"] += _delete_summaries(db, ids)
        counts["memories"] += delete_memories(db, owner_key)
        if ids:
            _clear_conversation_context(db, ids)

    return counts


__all__ = ["clear", "conversation_ids", "delete_memories", "owner_scope"]
