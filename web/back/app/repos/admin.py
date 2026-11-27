"""repos/admin.py —— 后台要用的那些查询（只读聚合 + 少量开关写）。

为什么单独一个文件：后台的查询形状和业务查询不一样 —— 它要的是"全站汇总"，
不是"某个用户的某条数据"。混进业务仓库里，两边都会变难读。

三条纪律：
1. **key 的明文永远不出这一层**：`list_keys` 只回 `key_enc` 之外的一切。
2. 统计用 SQL 聚合，不在 Python 里数（消息表迟早几十万行）。
3. 写操作只做"改一列"这种，复杂编辑属于业务层。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.clock import STATS_TZ_OFFSET_HOURS, utcnow
from app.models import (
    ApiKey,
    Character,
    CharacterPrompt,
    ConfigEntry,
    Conversation,
    Device,
    Message,
    UsageEvent,
    User,
    WorldbookEntry,
)


def counts(db: Session) -> dict[str, int]:
    """全站有多少东西。后台首页第一眼就看这个。"""
    return {
        "users": _count(db, User),
        "devices": _count(db, Device),
        "conversations": _count(db, Conversation),
        "messages": _count(db, Message),
        "characters": _count(db, Character),
        "worldbook": _count(db, WorldbookEntry),
    }


def _count(db: Session, model: type) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def key_stats(db: Session) -> dict[str, int]:
    """密钥池的健康度：总数 / 启用 / 冷却中 / 禁用。"""
    now = utcnow()
    total = _count(db, ApiKey)
    enabled = int(
        db.scalar(
            select(func.count()).select_from(ApiKey).where(ApiKey.enabled == 1)
        )
        or 0
    )
    cooling = int(
        db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.enabled == 1, ApiKey.cooldown_until.is_not(None))
            .where(ApiKey.cooldown_until > now)
        )
        or 0
    )
    return {"total": total, "enabled": enabled, "cooldown": cooling, "disabled": total - enabled}


def usage_summary(db: Session, since: datetime) -> dict[str, Any]:
    """某个时间点之后的用量汇总（首页的"今天"，边界由调用方按东八区算）。"""
    row = db.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0),
            func.coalesce(func.avg(UsageEvent.total_ms), 0),
        ).where(UsageEvent.created_at >= since)
    ).one()
    return {
        "requests": int(row[0] or 0),
        "prompt_tokens": int(row[1] or 0),
        "completion_tokens": int(row[2] or 0),
        "cached_tokens": int(row[3] or 0),
        "avg_ms": int(row[4] or 0),
    }


def _day_label() -> Any:
    """按天分组用的"日子"：东八区的日历日（`YYYY-MM-DD`）。

    库里存的是 naive UTC，所以先 +8 小时再 `date()` —— 和 `clock.day_start_utc()`
    是同一个边界，两边对不上就会出现"今日合计 ≠ 今天那根柱子"。
    """
    shifted = UsageEvent.created_at + text(f"INTERVAL {STATS_TZ_OFFSET_HOURS} HOUR")
    return func.date(shifted).label("d")


def usage_by_day(db: Session, since: datetime) -> list[dict[str, Any]]:
    """按天汇总（日界按东八区）。没有请求的那天不会出现在结果里，前端自己补零。"""
    day = _day_label()
    rows = db.execute(
        select(
            day,
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cached_tokens), 0),
            func.coalesce(func.avg(UsageEvent.total_ms), 0),
        )
        .where(UsageEvent.created_at >= since)
        .group_by(day)
        .order_by(day)
    ).all()
    return [
        {
            "date": str(row[0]),
            "requests": int(row[1] or 0),
            "prompt_tokens": int(row[2] or 0),
            "completion_tokens": int(row[3] or 0),
            "cached_tokens": int(row[4] or 0),
            "avg_ms": int(row[5] or 0),
        }
        for row in rows
    ]


def list_users(db: Session, *, q: str, limit: int, offset: int) -> tuple[list[User], int]:
    """用户列表（可按用户名/显示名搜）。返回 (这一页, 总数)。"""
    stmt = select(User)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.login_id.like(like), User.display_name.like(like)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(db.scalars(stmt.order_by(User.id.desc()).limit(limit).offset(offset)))
    return rows, total


def find_user(db: Session, public_id: str) -> User | None:
    return db.scalar(select(User).where(User.public_id == public_id))


def device_count_of(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Device).where(Device.owner_user_id == user_id)
        )
        or 0
    )


def list_characters(db: Session) -> list[tuple[Character, int]]:
    """全部角色（含下架）+ 当前提示词版本号。**每个角色只出现一行。**

    不能直接 join `character_prompts`：同一个角色在 v1/v2 两个变体里各有一条
    `active=1`，join 出来会变成 2 行（后台显示 72 行 / 36 个干员，就是这么来的）。
    这里用相关子查询只取 **v1**（默认变体）的最大版本号，天然一行。
    """
    prompt_ver = (
        select(func.max(CharacterPrompt.ver))
        .where(
            CharacterPrompt.character_id == Character.id,
            CharacterPrompt.active == 1,
            CharacterPrompt.variant == "v1",
        )
        .correlate(Character)
        .scalar_subquery()
    )
    rows = db.execute(
        select(Character, prompt_ver).order_by(Character.sort_order, Character.id)
    ).all()
    return [(row[0], int(row[1] or 0)) for row in rows]


def find_character(db: Session, slug: str) -> Character | None:
    """按 slug 找角色，**含已下架的**。业务层的 find_by_slug 会过滤掉下架的，
    后台要能改下架角色（比如重新上架），所以不能用那个。"""
    return db.scalar(select(Character).where(Character.slug == slug))


def list_config(db: Session) -> list[ConfigEntry]:
    return list(db.scalars(select(ConfigEntry).order_by(ConfigEntry.k)))


def set_config(db: Session, key: str, value: str) -> ConfigEntry:
    """写一条文本配置。`v` 是 JSON 列，这里统一存 JSON 字符串。"""
    entry = db.get(ConfigEntry, key)
    if entry is None:
        entry = ConfigEntry(k=key, v=value, updated_at=utcnow())
        db.add(entry)
    else:
        entry.v = value
        entry.updated_at = utcnow()
    db.flush()
    return entry


def list_keys(db: Session) -> list[ApiKey]:
    """密钥列表。**调用方只能取非密文字段** —— 明文不许出这一层。"""
    return list(db.scalars(select(ApiKey).order_by(ApiKey.id)))


def find_key(db: Session, key_id: int) -> ApiKey | None:
    return db.get(ApiKey, key_id)


def messages_with_reasoning(
    db: Session, conversation_id: int, *, limit: int
) -> list[Message]:
    """排障用：最近若干条**带思考过程**的消息。"""
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.reasoning.is_not(None))
        .order_by(Message.seq.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def find_conversation(db: Session, public_id: str) -> Conversation | None:
    return db.scalar(select(Conversation).where(Conversation.public_id == public_id))


__all__ = [
    "counts",
    "device_count_of",
    "find_character",
    "find_conversation",
    "find_key",
    "find_user",
    "key_stats",
    "list_characters",
    "list_config",
    "list_keys",
    "list_users",
    "messages_with_reasoning",
    "set_config",
    "usage_by_day",
    "usage_summary",
]
