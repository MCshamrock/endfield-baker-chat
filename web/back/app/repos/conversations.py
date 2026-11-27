"""repos/conversations.py —— 会话表的所有 SQL。

四个要点，改这个文件前先看一遍：

1. **"归属"有两种，别写成一种**。
   * 访客：数据属于一台设备（`device_id`，且 `user_id IS NULL`）
   * 已登录：数据属于账号（`user_id`），同一账号下任何设备都看得见

   所以每个"按归属查"的函数都要同时收 `user_id` / `device_id`，由 `_owner_scope`
   拼出条件。这样"别人的会话"在 SQL 层就不存在，而不是查出来再 if 判断
   （那种写法迟早漏一处）。

2. **`lock_owned` 才允许分配 seq**。它用 `SELECT ... FOR UPDATE` 把会话行锁住，
   同一会话的并发写会排队，`next_seq` 才不会撞。

3. **软删**。`deleted_at` 不为空就算删了，行还在（便于排查"用户说记录没了"）。
   所有查询都要带 `deleted_at IS NULL`。

4. **登录时会把该设备名下的会话 `user_id` 补上**（见 api/auth.py），
   这就是"访客转登录不搬数据"的全部秘密：只改一列。
"""

from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.ids import new_public_id
from app.models import Character, Conversation, Message


def _owner_scope(user_id: int | None, device_id: int) -> ColumnElement[bool]:
    """"这条数据是不是我的"。

    登录态按账号判 —— 同一账号下换台设备也看得见；
    访客态按设备判，并且要求 `user_id IS NULL`：这台设备以前登录过别人，
    那些带 user_id 的记录不属于现在的访客，不能露给他。
    """
    if user_id is not None:
        return Conversation.user_id == user_id
    return (Conversation.device_id == device_id) & Conversation.user_id.is_(None)


def create(
    db: Session,
    *,
    device_id: int,
    character_id: int | None,
    mode: str,
    title: str,
    user_id: int | None = None,
    last_active_at: datetime | None = None,
    import_batch_id: int | None = None,
) -> Conversation:
    """建一个会话。next_seq 从 0 开始（第一条消息是 1）。

    `user_id` 非空 = 这个会话属于账号（登录态建的），所有设备可见。
    `last_active_at` 正常不用传（= 现在）；只有导入历史数据时要造一个"过去的时间"，
    会话列表才按老包里的顺序排，而不是全部挤在同一秒。
    `import_batch_id` 记下"这条会话是哪次导入建的"，撤销导入时按它找。
    """
    now = utcnow()
    active_at = last_active_at or now
    conversation = Conversation(
        public_id=new_public_id(),
        device_id=device_id,
        user_id=user_id,
        character_id=character_id,
        mode=mode,
        title=title,
        next_seq=0,
        import_batch_id=import_batch_id,
        created_at=now,
        last_active_at=active_at,
    )
    db.add(conversation)
    db.flush()
    return conversation


def list_for_owner(
    db: Session, *, user_id: int | None, device_id: int, limit: int = 200
) -> list[tuple[Conversation, str | None]]:
    """归属者能看到的所有会话，最近活跃的在前。

    返回 (会话, 角色的 slug)。带 slug 是因为客户端认的是 slug，不是自增 id；
    在 SQL 里 join 一次，比回来再逐个查角色表省事。
    """
    stmt = (
        select(Conversation, Character.slug)
        .outerjoin(Character, Character.id == Conversation.character_id)
        .where(
            _owner_scope(user_id, device_id),
            Conversation.deleted_at.is_(None),
        )
        .order_by(Conversation.last_active_at.desc(), Conversation.id.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def count_for_owner(
    db: Session, *, user_id: int | None, device_id: int
) -> tuple[int, int]:
    """归属者名下有多少（会话, 消息）。

    只数不取内容 —— 它唯一的用途是回答"这个人是不是已经有聊天记录"，
    客户端拿它决定"要不要先把人拦下来登录"（有记录却不登录 = 换台设备就找不着）。
    用 COUNT 而不是把列表拉回来数一遍：老设备上几百条会话时差别很明显。
    """
    conv_count = db.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(_owner_scope(user_id, device_id), Conversation.deleted_at.is_(None))
    )
    msg_count = db.scalar(
        select(func.count())
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(_owner_scope(user_id, device_id), Conversation.deleted_at.is_(None))
    )
    return int(conv_count or 0), int(msg_count or 0)


def find_owned(
    db: Session, public_id: str, *, user_id: int | None, device_id: int
) -> tuple[Conversation, str | None] | None:
    """按 public_id 找归属者自己的会话（带 slug）。别人的/已删的都会返回 None。"""
    stmt = (
        select(Conversation, Character.slug)
        .outerjoin(Character, Character.id == Conversation.character_id)
        .where(
            Conversation.public_id == public_id,
            _owner_scope(user_id, device_id),
            Conversation.deleted_at.is_(None),
        )
    )
    row = db.execute(stmt).first()
    if row is None:
        return None
    return row[0], row[1]


def lock_owned(
    db: Session, public_id: str, *, user_id: int | None, device_id: int
) -> Conversation | None:
    """锁住归属者的会话行（SELECT ... FOR UPDATE），供分配 seq 用。

    锁必须在事务提交或回滚后才释放。所以调用方拿到它之后要尽快完成写入并 commit，
    别在拿着锁的时候去等网络（例如调用上游模型）—— 那会把整个会话卡住。
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.public_id == public_id,
            _owner_scope(user_id, device_id),
            Conversation.deleted_at.is_(None),
        )
        .with_for_update()
        # populate_existing 必须加：调用方通常已经在本事务里读过这个会话
        # （`find_owned`），SQLAlchemy 会把 identity map 里那个旧对象原样返回、
        # 不回填数据库最新值 → `next_seq` 用过期的值分配 seq，并发时撞唯一键 500。
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def find_by_public_id(db: Session, public_id: str) -> Conversation | None:
    """只按 public_id 找会话（不带设备条件）。

    唯一用途：流式回复结束时，用一个新的数据库会话把结果写回去 ——
    那时候手里只剩 public_id，没有 device 对象，也不该再信任那个旧会话。
    """
    return db.scalar(
        select(Conversation).where(
            Conversation.public_id == public_id,
            Conversation.deleted_at.is_(None),
        )
    )


def create_members(
    db: Session, *, conversation_id: int, character_ids: list[int], player_slug: str | None = None
) -> None:
    """群聊成员：按给定顺序写进 conversation_members（ord 就是发言顺序）。"""
    from app.models import ConversationMember

    for index, character_id in enumerate(character_ids):
        db.add(
            ConversationMember(
                conversation_id=conversation_id,
                character_id=character_id,
                ord=index,
                role="ai",
            )
        )
    db.flush()


def list_members(db: Session, conversation_id: int) -> list[tuple[int, str, str, int]]:
    """返回 [(character_id, slug, name, ord)]，按发言顺序排。"""
    from app.models import Character, ConversationMember

    stmt = (
        select(ConversationMember, Character)
        .join(Character, Character.id == ConversationMember.character_id)
        .where(ConversationMember.conversation_id == conversation_id)
        .order_by(ConversationMember.ord)
    )
    return [
        (member.character_id, character.slug, character.name, member.ord)
        for member, character in db.execute(stmt).all()
    ]


def members_by_conversation(
    db: Session, conversation_ids: list[int]
) -> dict[int, list[str]]:
    """一次查出多组群聊的成员名（按发言顺序）。

    列表页要给每个群聊画头像叠层，逐条调 `list_members` 就是 N+1 次查询 ——
    会话一多，一个列表请求能打出上百次 SQL。这里一次查完。
    """
    from app.models import Character, ConversationMember

    if not conversation_ids:
        return {}
    stmt = (
        select(ConversationMember.conversation_id, Character.name)
        .join(Character, Character.id == ConversationMember.character_id)
        .where(ConversationMember.conversation_id.in_(conversation_ids))
        .order_by(ConversationMember.conversation_id, ConversationMember.ord)
    )
    grouped: dict[int, list[str]] = {}
    for conversation_id, name in db.execute(stmt).all():
        grouped.setdefault(conversation_id, []).append(name)
    return grouped


def touch(db: Session, conversation: Conversation) -> None:
    """更新"最后活跃时间"。会话列表按这个排序，所以每次写消息都要刷一下。"""
    conversation.last_active_at = utcnow()
    db.flush()


def soft_delete(db: Session, conversation: Conversation) -> None:
    """软删。行留着，排查"用户说记录没了"时能看到它确实被删过、什么时候删的。"""
    conversation.deleted_at = utcnow()
    db.flush()


def list_by_batch(db: Session, device_id: int, import_batch_id: int) -> list[Conversation]:
    """某次导入建出来的会话（撤销导入的范围就是这个集合）。"""
    stmt = select(Conversation).where(
        Conversation.device_id == device_id,
        Conversation.import_batch_id == import_batch_id,
        Conversation.deleted_at.is_(None),
    )
    return list(db.scalars(stmt).all())


def soft_delete_all(db: Session, *, user_id: int | None, device_id: int) -> int:
    """把归属者的会话全部软删，返回删了几条。

    只给导入的"覆盖模式"用：老客户端导入时就是这个语义（覆盖 = 替换全部数据），
    这里保持一致，而不是自作聪明地做成"合并"。
    """
    rows = list(
        db.scalars(
            select(Conversation).where(
                _owner_scope(user_id, device_id),
                Conversation.deleted_at.is_(None),
            )
        ).all()
    )
    now = utcnow()
    for conversation in rows:
        conversation.deleted_at = now
    db.flush()
    return len(rows)


def adopt_owned(db: Session, *, device_id: int, user_id: int) -> int:
    """把"还没归属账号"的会话挂到账号名下，返回改了几条。

    这是登录的核心动作，只有一句 UPDATE，却解决了整个"访客转登录"的问题：
    登录前在设备上聊的东西不用搬、不用复制，改一列 `user_id` 就变成账号的数据，
    从此任何一台登录了同一账号的设备都看得见。

    只动 `user_id IS NULL` 的行：这台设备如果之前属于别的账号（用户登出后换号登录），
    那些会话已经名花有主，不能再划给别人。
    """
    stmt = select(Conversation).where(
        Conversation.device_id == device_id,
        Conversation.user_id.is_(None),
    )
    rows = list(db.scalars(stmt).all())
    for conversation in rows:
        conversation.user_id = user_id
    db.flush()
    return len(rows)
