"""repos/content.py —— 服务端要喂给模型的"文本内容"从哪来。

三类：
  * 安全协议（每轮最前面那段）—— `config` 表，key = `safety_protocol`，**带 60 秒缓存**
  * 世界观（全局那一段）—— `config` 表，key = `worldview`
  * 角色提示词 —— `character_prompts` 表，按「变体（variant）+ active」取一行

角色提示词有两套变体（2026-11 加）：
  * `v1`：老包里的常规长卡，所有老路径（`/v1/conversations/*` 等）默认用它；
  * `v2`：实验性长卡，只有老客户端把 `use_new_prompt` 打开时才用。
  v2 缺失时回落到 v1 —— 这是老后端的行为，不能变（否则某些角色会突然没台词）。

都做成"查库"，不是"读文件"：改它们不该需要发版或重启，版本化还意味着能回退。

为什么只有安全协议带缓存：它每轮都要取、而且基本不变；世界观虽然也每轮取，
但和它一样是"一条按主键查的单行查询"，命中的是 InnoDB 缓冲池，开销可忽略。
角色提示词同理**故意不缓存**：查询走的是 (character_id, variant, active) 索引，
每轮一次不值得缓存；而缓存会让"刚导入的 v2 一分钟内取不到"，还多一类
"v2 的内容串到 v1 上"的坏法。将来真要加缓存，键必须是 (character_id, variant)
两段一起，并且 `clear_cache()` 要连它一起清。
"""

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import CharacterPrompt, ConfigEntry, DevicePrompt

logger = logging.getLogger("baker.content")

# 安全协议缓存：{ key: (正文, 过期时间) }
_SAFETY_TTL_SECONDS = 60.0
_safety_cache: dict[str, tuple[str, float]] = {}


def get_text_config(db: Session, key: str, default: str = "") -> str:
    """读一条文本配置。

    `config.v` 是 JSON 列，写入时用字符串字面量（JSON string），这里原样取回。
    取不到就返回 default —— 调用方不用自己判 None。
    """
    entry = db.get(ConfigEntry, key)
    if entry is None or entry.v is None:
        return default
    value = entry.v
    return value if isinstance(value, str) else str(value)


def get_safety_protocol(db: Session) -> str:
    """取安全协议（带 60 秒缓存）。

    缓存的意义：这一段每轮都要拼进 prompt，但它几乎从不改；
    60 秒的延迟可以接受（改完一分钟内生效），换来的是每轮省一次查询。

    改完想立刻生效：后台改配置时调 `clear_cache()`，或者等 60 秒。
    """
    cached = _safety_cache.get("safety_protocol")
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]

    text = get_text_config(db, "safety_protocol")
    _safety_cache["safety_protocol"] = (text, now + _SAFETY_TTL_SECONDS)
    return text


def clear_cache() -> None:
    """清掉本模块的缓存（测试和后台改配置时用）。

    ⚠️ 以后如果给角色提示词加了缓存，**必须在这里一起清**。
    现在故意没加（原因见文件头），所以这里只有安全协议一张表。
    """
    _safety_cache.clear()


def get_active_prompt(db: Session, character_id: int, variant: str = "v1") -> str:
    """取某个角色、某个变体下当前生效的提示词正文。没有就返回空串。

    参数 `variant`：
      * "v1"（默认）—— 老包常规长卡。不传就是它，所以所有老路径行为不变。
      * "v2" —— 实验性长卡。只有当老客户端把 `use_new_prompt` 打开时才会传进来。

    **v2 取不到就回落 v1**：和老后端一致 —— characters_v2/ 里没有这个角色时，
    老后端只记一条 warning 然后读 characters/。不回落的话，一个还没写 v2 的角色
    会突然变成"没有人设"，那是最糟的降级方式。

    回落只记 debug 日志：这是正常情况（本来就允许 v2 只覆盖一部分角色），
    不该刷 warning 把日志淹掉；真要排查"为什么这个角色不像 v2"，把日志调到 DEBUG 即可。
    """
    body = _select_active_body(db, character_id, variant)
    if body:
        return body
    if variant != "v1":
        logger.debug("角色 %s 没有 %s 提示词，回落到 v1", character_id, variant)
        return _select_active_body(db, character_id, "v1")
    return ""


def _select_active_body(db: Session, character_id: int, variant: str) -> str:
    """真正的单行查询：同一角色 + 同一变体里，取 active=1 且版本号最大的那一行。"""
    stmt = (
        select(CharacterPrompt.body)
        .where(
            CharacterPrompt.character_id == character_id,
            CharacterPrompt.variant == variant,
            CharacterPrompt.active == 1,
        )
        .order_by(CharacterPrompt.ver.desc())
        .limit(1)
    )
    return db.scalar(stmt) or ""


def get_character_prompt(
    db: Session, character_id: int, device_id: int | None = None, variant: str = "v1"
) -> str:
    """这个角色**对这台设备**、**这一套变体**该用哪段提示词。

    优先级：设备自己改过的（`device_prompts`）> 全局生效版本。
    设备级覆盖来自老客户端的数据包（用户自己写过的人设），只影响这台设备 ——
    别人的角色不会因为某个人改过而变样。

    `device_id=None`（老客户端兼容层没有设备概念）时直接走全局版本。
    `variant` 只影响"全局版本"这一层：设备覆盖是一段自己的正文，不分 v1/v2。

    注意：设备覆盖命中时**不再看 variant** —— 用户亲手改过的文本优先于任何预设，
    这也和老客户端的直觉一致（我自己写的人设，不能因为开了新提示词就被换掉）。
    """
    if device_id is not None:
        body = db.scalar(
            select(DevicePrompt.body).where(
                DevicePrompt.device_id == device_id,
                DevicePrompt.character_id == character_id,
            )
        )
        if body:
            return body
    return get_active_prompt(db, character_id, variant)


def upsert_device_prompt(db: Session, *, device_id: int, character_id: int, body: str) -> None:
    """写入/覆盖设备级提示词（导入数据包时用）。同设备同角色只留一条。"""
    row = db.scalar(
        select(DevicePrompt).where(
            DevicePrompt.device_id == device_id,
            DevicePrompt.character_id == character_id,
        )
    )
    if row is None:
        db.add(
            DevicePrompt(
                device_id=device_id,
                character_id=character_id,
                body=body,
                created_at=utcnow(),
            )
        )
    else:
        row.body = body
    db.flush()


__all__ = [
    "clear_cache",
    "get_active_prompt",
    "get_character_prompt",
    "get_safety_protocol",
    "get_text_config",
    "upsert_device_prompt",
]
