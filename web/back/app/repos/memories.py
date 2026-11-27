"""repos/memories.py —— 干员记忆的读写（跨会话的人情账）。

这里的每一条规则都对应一个真实会翻车的地方：

  * **指纹唯一**：同一件事被压两遍、被两个设备各记一遍，都只会有一条。
  * **单值 kind 自动覆盖**：`nickname` 只能有一个。否则 A 说叫阿哲、B 说叫老陈，
    两条都留着，模型看到两个名字 —— 要么随机挑，要么和稀泥。
  * **删过的不许复活**：删除是软删 + 留指纹，写入时见到 `deleted` 就跳过。
    不加这条，"让我忘掉"就是句空话。
  * **容量靠淘汰，不靠截断**：超过上限时把最久没用、且没置顶的那条标 `superseded`。

一句话：这些是**账**，不是缓存。写进来容易，改口径很难。
"""

import hashlib
import re

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import CharacterMemory, CharacterWeight, MemorySettings

# 封闭枚举：模型只能在这 8 类里选，不许自造。新增一类要发版。
KINDS = (
    "nickname",
    "call",
    "appearance",
    "relation",
    "preference",
    "dislike",
    "promise",
    "fact",
)

# 单值：同一个玩家 × 同一个干员只留一条，新的顶掉旧的
SINGLE_KINDS = ("nickname", "call", "appearance", "relation")

# 每个干员的账本上限（active 条目）。超了淘汰最久没用的那条。
MAX_ENTRIES = 40
# 单条上限。超过说明这条混了两件事，该让模型拆开写。
MAX_TEXT = 300

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!~…—\-—\"'“”‘’（）()【】\[\]]+")


def fingerprint(owner_key: str, character_id: int, kind: str, text: str) -> str:
    """去重指纹：归一化文本后取 sha256 前 16 位。

    归一化只做"去标点空白"——不做同义词替换，也不做 lower（中文没有大小写问题）。
    语义相同但换了说法的两条，靠提炼时让模型自己去看已有条目，而不是靠指纹。
    """
    normalized = _PUNCT.sub("", text.strip())
    raw = f"{owner_key}|{character_id}|{kind}|{normalized}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def list_active(
    db: Session, *, owner_key: str, character_id: int, limit: int | None = None
) -> list[CharacterMemory]:
    """取生效中的条目。置顶优先、再按最近更新排。"""
    stmt = (
        select(CharacterMemory)
        .where(
            CharacterMemory.owner_key == owner_key,
            CharacterMemory.character_id == character_id,
            CharacterMemory.status == "active",
        )
        .order_by(
            CharacterMemory.pinned.desc(),
            CharacterMemory.updated_at.desc(),
            CharacterMemory.id.desc(),
        )
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


def find(
    db: Session, *, owner_key: str, character_id: int, fingerprint_value: str
) -> CharacterMemory | None:
    """按指纹找一条（不管什么状态 —— 连 deleted 也要能找出来）。"""
    return db.scalar(
        select(CharacterMemory).where(
            CharacterMemory.owner_key == owner_key,
            CharacterMemory.character_id == character_id,
            CharacterMemory.fingerprint == fingerprint_value,
        )
    )


def add(
    db: Session,
    *,
    owner_key: str,
    character_id: int,
    kind: str,
    text: str,
    source_conversation_id: int | None = None,
) -> CharacterMemory | None:
    """写入一条。返回新行；跳过（重复/被删过/不合规）时返回 None。

    调用方负责提交事务。
    """
    body = (text or "").strip()
    if kind not in KINDS or not body:
        return None
    body = body[:MAX_TEXT]

    fp = fingerprint(owner_key, character_id, kind, body)
    existing = find(
        db, owner_key=owner_key, character_id=character_id, fingerprint_value=fp
    )
    if existing is not None:
        if existing.status == "deleted":
            # 用户亲手删的，不许再写回来
            return None
        if existing.status == "active":
            existing.updated_at = utcnow()
            db.flush()
            return existing

    now = utcnow()
    row = CharacterMemory(
        owner_key=owner_key,
        character_id=character_id,
        kind=kind,
        text=body,
        fingerprint=fp,
        status="active",
        source_conversation_id=source_conversation_id,
        pinned=0,
        created_at=now,
        updated_at=now,
        last_used_at=now,
    )
    db.add(row)
    db.flush()

    if kind in SINGLE_KINDS:
        _supersede_kind(db, owner_key, character_id, kind, keep_id=row.id)
    _evict_if_full(db, owner_key, character_id)
    return row


def _supersede_kind(
    db: Session, owner_key: str, character_id: int, kind: str, *, keep_id: int
) -> None:
    """单值 kind：把同类的其它 active 条目标成 superseded。"""
    others = db.scalars(
        select(CharacterMemory).where(
            CharacterMemory.owner_key == owner_key,
            CharacterMemory.character_id == character_id,
            CharacterMemory.kind == kind,
            CharacterMemory.status == "active",
            CharacterMemory.id != keep_id,
        )
    )
    now = utcnow()
    for other in others:
        other.status = "superseded"
        other.supersedes_id = keep_id
        other.updated_at = now
    db.flush()


def supersede(db: Session, *, owner_key: str, memory_id: int, by_id: int | None = None) -> bool:
    """把一条标成失效（被新条目推翻）。"""
    row = db.get(CharacterMemory, memory_id)
    if row is None or row.owner_key != owner_key or row.status != "active":
        return False
    row.status = "superseded"
    row.supersedes_id = by_id
    row.updated_at = utcnow()
    db.flush()
    return True


def _evict_if_full(db: Session, owner_key: str, character_id: int) -> None:
    """超过上限时淘汰最久没被用到的那条（置顶的不动）。"""
    rows = list_active(db, owner_key=owner_key, character_id=character_id)
    overflow = len(rows) - MAX_ENTRIES
    if overflow <= 0:
        return
    candidates = [row for row in rows if not row.pinned]
    candidates.sort(key=lambda row: (row.last_used_at, row.id))
    now = utcnow()
    for row in candidates[:overflow]:
        row.status = "superseded"
        row.updated_at = now
    db.flush()


def touch_used(db: Session, rows: list[CharacterMemory]) -> None:
    """记一次"这条被注入了"。淘汰顺序靠它，否则老条目永远赖着。"""
    now = utcnow()
    for row in rows:
        row.last_used_at = now
    if rows:
        db.flush()


def remove(db: Session, *, owner_key: str, memory_id: int) -> bool:
    """软删（留指纹，防复活）。删的也包括同指纹的其它行。"""
    row = db.get(CharacterMemory, memory_id)
    if row is None or row.owner_key != owner_key or row.status == "deleted":
        return False

    twins = db.scalars(
        select(CharacterMemory).where(
            CharacterMemory.owner_key == owner_key,
            CharacterMemory.character_id == row.character_id,
            CharacterMemory.fingerprint == row.fingerprint,
        )
    )
    now = utcnow()
    for twin in twins:
        twin.status = "deleted"
        twin.updated_at = now
    db.flush()
    return True


def patch(
    db: Session, *, owner_key: str, memory_id: int, text: str | None, pinned: bool | None
) -> CharacterMemory | None:
    """改文本 / 置顶。文本变了要重算指纹（它是一条不同的记忆了）。"""
    row = db.get(CharacterMemory, memory_id)
    if row is None or row.owner_key != owner_key or row.status == "deleted":
        return None

    if text is not None:
        body = text.strip()[:MAX_TEXT]
        if not body:
            return None
        row.text = body
        row.fingerprint = fingerprint(owner_key, row.character_id, row.kind, body)
    if pinned is not None:
        row.pinned = 1 if pinned else 0
    row.updated_at = utcnow()
    db.flush()
    return row


# ---------------------------------------------------------------- 开关
def get_mode(db: Session, owner_key: str) -> str:
    """玩家级默认开关。没有行就是 on。"""
    row = db.get(MemorySettings, owner_key)
    return row.mode if row is not None else "on"


def set_mode(db: Session, owner_key: str, mode: str) -> str:
    if mode not in ("on", "off"):
        raise ValueError("mode 只能是 on / off")
    row = db.get(MemorySettings, owner_key)
    if row is None:
        db.add(MemorySettings(owner_key=owner_key, mode=mode, updated_at=utcnow()))
    else:
        row.mode = mode
        row.updated_at = utcnow()
    db.flush()
    return mode


def resolve_enabled(db: Session, *, owner_key: str, conversation_mode: str) -> bool:
    """这一局到底要不要读记忆：会话覆盖 > 全局默认。"""
    if conversation_mode == "on":
        return True
    if conversation_mode == "off":
        return False
    return get_mode(db, owner_key) == "on"


# ---------------------------------------------------------------- 发言权重
def weights_for(
    db: Session, *, owner_key: str, character_ids: list[int]
) -> dict[int, int]:
    """玩家自定义的权重覆盖：{character_id: active}。没覆盖的不会出现在返回值里。"""
    if not character_ids:
        return {}
    rows = db.scalars(
        select(CharacterWeight).where(
            CharacterWeight.owner_key == owner_key,
            CharacterWeight.character_id.in_(character_ids),
        )
    )
    return {row.character_id: row.active for row in rows}


def set_weight(db: Session, *, owner_key: str, character_id: int, active: int) -> int:
    """写玩家对某个干员的权重覆盖。**必须原子**。

    以前是"先 SELECT 再 INSERT/UPDATE"，两个人同时点保存时：
      * 两边都 SELECT 到 None → 都 INSERT → 撞主键 500；
      * 或一边 DELETE 一边 UPDATE → `UPDATE ... 0 rows matched` → StaleDataError 500。
    主键就是 `(owner_key, character_id)`，所以直接用 MySQL 的 upsert，
    一条语句搞定插入/更新，不再有"读-改-写"窗口。
    """
    value = max(1, min(100, int(active)))
    stmt = mysql_insert(CharacterWeight).values(
        owner_key=owner_key,
        character_id=character_id,
        active=value,
        updated_at=utcnow(),
    )
    stmt = stmt.on_duplicate_key_update(
        active=stmt.inserted.active,
        updated_at=stmt.inserted.updated_at,
    )
    db.execute(stmt)
    return value


def delete_weight(db: Session, *, owner_key: str, character_id: int) -> bool:
    """删掉这个玩家对某个干员的覆盖行（= 恢复角色默认权重）。

    返回 `True` 表示真删了一行，`False` 表示本来就没有覆盖。
    **没覆盖也算成功** —— 客户端点「恢复默认」时，不该因为"你本来就没改过"报错。

    删除是物理 DELETE（不是软删）：这张表只是"玩家的个人设置"，
    删了就等于"没设置过"，没有需要保留的历史。下次再 PUT 会重新插一行。
    """
    # 直接一条 DELETE 拿 rowcount：先 SELECT 再 ORM delete 在并发下会
    # 因为"行已经被别人删了"抛 StaleDataError（0 rows matched）。
    result = db.execute(
        sa_delete(CharacterWeight).where(
            CharacterWeight.owner_key == owner_key,
            CharacterWeight.character_id == character_id,
        )
    )
    return (result.rowcount or 0) > 0


def effective_weights(
    db: Session, *, owner_key: str | None, defaults: dict[int, int]
) -> dict[int, tuple[int, str]]:
    """每个干员的**有效**权重 + 它从哪来：`{character_id: (weight, source)}`。

    规则只有一条，和群聊选人（`app/group.py` 的 `_weights`）完全一致：
    **玩家覆盖优先，否则用角色自带的 `active_weight`。**

    参数：
      * `defaults`：调用方先把 `characters.active_weight` 查好传进来，`{id: 默认值}`
      * `owner_key`：`u:<user_id>` / `d:<device_id>`；传 None 表示"不知道是谁"
        （例如游客没带 token），这时全部按默认值，连覆盖表都不查。

    `source` 只有两种值：
      * `"user"`    —— 玩家在 `character_weights` 里改过
      * `"default"` —— 没改过，用角色自带值
    """
    overrides = (
        weights_for(db, owner_key=owner_key, character_ids=list(defaults))
        if owner_key
        else {}
    )
    result: dict[int, tuple[int, str]] = {}
    for character_id, default in defaults.items():
        if character_id in overrides:
            result[character_id] = (overrides[character_id], "user")
        else:
            result[character_id] = (default, "default")
    return result


__all__ = [
    "KINDS",
    "MAX_ENTRIES",
    "SINGLE_KINDS",
    "add",
    "delete_weight",
    "effective_weights",
    "fingerprint",
    "get_mode",
    "list_active",
    "remove",
    "resolve_enabled",
    "set_mode",
    "set_weight",
    "supersede",
    "touch_used",
    "patch",
    "weights_for",
]
