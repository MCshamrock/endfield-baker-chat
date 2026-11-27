"""api/admin.py —— 后台数据接口（`/v1/admin/*`）。只给数据，页面交给前端。

设计口径（和 shamrockai 后台一套思路）：

  * 一个 **overview** 端点把"第一眼要看的数"全给齐（站内规模 / 密钥池 / 今日用量 / 近 7 天）；
  * 其余都是**列表 + 单条修改**，不做花哨的批量操作 —— 批量一次做错就是全站事故；
  * 所有响应都是**扁平 JSON**，字段名稳定，前端按它画表；
  * 密钥**只回非密文字段**，明文永远不出服务端；
  * 每个写操作只写一列，改完立刻生效（配置那层会顺手清缓存）。

鉴权：`CurrentAdmin` —— 必须登录且 `role='admin'`。设备 token 就能用，
不需要另开一套后台登录。

第一个管理员怎么来：`.venv/Scripts/python.exe scripts/make_admin.py --username 你的用户名`
"""

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import errors
from app.clock import day_start_utc, to_iso_str, utcnow
from app.deps import CurrentAdmin, DbSession
from app.repos import admin as admin_repo
from app.repos import content as content_repo

router = APIRouter(tags=["admin"])
logger = logging.getLogger("baker.admin")


# ---------------------------------------------------------------- 请求体
class UserPatch(BaseModel):
    """改一个账号。三个字段都可选，只传要改的。"""

    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    role: str | None = Field(default=None, pattern="^(user|admin)$")
    display_name: str | None = Field(default=None, max_length=64)


class CharacterPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, max_length=64)
    active_weight: int | None = Field(default=None, ge=1, le=100)
    sort_order: int | None = None


class ConfigPut(BaseModel):
    """配置值统一按字符串存（`config.v` 是 JSON 列，存的是一段文本）。"""

    value: str = Field(max_length=200_000)


class KeyPatch(BaseModel):
    enabled: bool | None = None


# ---------------------------------------------------------------- 概览
@router.get("/admin/overview")
def overview(db: DbSession, _admin: CurrentAdmin) -> dict[str, Any]:
    """后台首页：规模 + 密钥池 + 今日 + 近 7 天。日界按东八区切（不是 UTC）。"""
    today = day_start_utc()
    return {
        "counts": admin_repo.counts(db),
        "keys": admin_repo.key_stats(db),
        "today": admin_repo.usage_summary(db, today),
        "days": admin_repo.usage_by_day(db, day_start_utc(6)),
        "now": to_iso_str(utcnow()),
    }


# ---------------------------------------------------------------- 用户
@router.get("/admin/users")
def list_users(
    db: DbSession,
    _admin: CurrentAdmin,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """账号列表。`q` 同时搜用户名和显示名。"""
    limit = max(1, min(limit, 200))
    rows, total = admin_repo.list_users(db, q=q, limit=limit, offset=max(0, offset))
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": row.public_id,
                "username": row.login_id or "",
                "display_name": row.display_name,
                "status": row.status,
                "role": row.role,
                "devices": admin_repo.device_count_of(db, row.id),
                "created_at": to_iso_str(row.created_at),
                "last_seen_at": to_iso_str(row.last_seen_at) if row.last_seen_at else None,
            }
            for row in rows
        ],
    }


@router.patch("/admin/users/{public_id}")
def patch_user(
    public_id: str, req: UserPatch, db: DbSession, admin: CurrentAdmin
) -> dict[str, Any]:
    """改状态 / 提权 / 改显示名。**不能把自己降权**，免得把自己锁在门外。"""
    row = admin_repo.find_user(db, public_id)
    if row is None:
        raise errors.AppError("no_user", "没有这个账号", http_status=404)

    if req.role is not None and row.id == admin.id and req.role != "admin":
        raise errors.AppError(
            "cannot_demote_self", "不能取消自己的管理员权限", http_status=400
        )

    if req.status is not None:
        row.status = req.status
    if req.role is not None:
        row.role = req.role
    if req.display_name is not None:
        row.display_name = req.display_name.strip()
    db.commit()
    logger.info("后台改账号 %s role=%s status=%s", public_id, row.role, row.status)
    return {
        "id": row.public_id,
        "username": row.login_id or "",
        "display_name": row.display_name,
        "status": row.status,
        "role": row.role,
    }


# ---------------------------------------------------------------- 角色
@router.get("/admin/characters")
def list_characters(db: DbSession, _admin: CurrentAdmin) -> dict[str, Any]:
    """全部干员（含已下架）+ 当前提示词版本。"""
    rows = admin_repo.list_characters(db)
    return {
        "items": [
            {
                "id": character.slug,
                "name": character.name,
                "avatar": character.avatar,
                "attribute": character.attribute,
                "enabled": bool(character.enabled),
                "active_weight": character.active_weight,
                "sort_order": character.sort_order,
                "prompt_ver": ver,
            }
            for character, ver in rows
        ]
    }


@router.patch("/admin/characters/{slug}")
def patch_character(
    slug: str, req: CharacterPatch, db: DbSession, _admin: CurrentAdmin
) -> dict[str, Any]:
    """上下架 / 改名 / 调发言权重 / 调排序。"""
    character = admin_repo.find_character(db, slug)
    if character is None:
        raise errors.no_character()

    if req.enabled is not None:
        character.enabled = 1 if req.enabled else 0
    if req.name is not None and req.name.strip():
        character.name = req.name.strip()
    if req.active_weight is not None:
        character.active_weight = req.active_weight
    if req.sort_order is not None:
        character.sort_order = req.sort_order
    db.commit()
    return {
        "id": character.slug,
        "name": character.name,
        "enabled": bool(character.enabled),
        "active_weight": character.active_weight,
        "sort_order": character.sort_order,
    }


# ---------------------------------------------------------------- 配置
@router.get("/admin/config")
def list_config(db: DbSession, _admin: CurrentAdmin) -> dict[str, Any]:
    """所有可热改的文本配置（安全协议、世界观…）。"""
    rows = admin_repo.list_config(db)
    return {
        "items": [
            {"key": row.k, "value": row.v, "updated_at": to_iso_str(row.updated_at)}
            for row in rows
        ]
    }


@router.put("/admin/config/{key}")
def put_config(
    key: str, req: ConfigPut, db: DbSession, _admin: CurrentAdmin
) -> dict[str, Any]:
    """写一条配置。**写完立刻清缓存**，否则安全协议要等 60 秒才生效。"""
    if not key.strip():
        raise errors.bad_request("配置名不能为空")
    entry = admin_repo.set_config(db, key.strip(), req.value)
    db.commit()
    content_repo.clear_cache()
    return {"key": entry.k, "value": entry.v, "updated_at": to_iso_str(entry.updated_at)}


# ---------------------------------------------------------------- 密钥
@router.get("/admin/keys")
def list_keys(db: DbSession, _admin: CurrentAdmin) -> dict[str, Any]:
    """密钥池。**只回状态，不回密钥本身**（密文也不回，前端用不上）。"""
    rows = admin_repo.list_keys(db)
    now = utcnow()
    return {
        "items": [
            {
                "id": row.id,
                "label": row.label,
                "enabled": bool(row.enabled),
                "cooling": bool(
                    row.cooldown_until is not None and row.cooldown_until > now
                ),
                "cooldown_until": to_iso_str(row.cooldown_until) if row.cooldown_until else None,
                "fail_count": row.fail_count,
                "rpm": row.rpm,
                "base_url": row.base_url,
                "model": row.model,
                "last_used_at": to_iso_str(row.last_used_at) if row.last_used_at else None,
            }
            for row in rows
        ]
    }


@router.patch("/admin/keys/{key_id}")
def patch_key(key_id: int, req: KeyPatch, db: DbSession, _admin: CurrentAdmin) -> dict[str, Any]:
    """启停一把 key。禁用是"下次别再借它"，不是踢掉正在跑的请求。"""
    row = admin_repo.find_key(db, key_id)
    if row is None:
        raise errors.AppError("no_key", "没有这把密钥", http_status=404)
    if req.enabled is not None:
        row.enabled = 1 if req.enabled else 0
    db.commit()
    return {"id": row.id, "label": row.label, "enabled": bool(row.enabled)}


# ---------------------------------------------------------------- 用量
@router.get("/admin/usage")
def usage(db: DbSession, _admin: CurrentAdmin, days: int = 7) -> dict[str, Any]:
    """按天汇总，日界按东八区（UTC+8）切。`days` 最大 90。"""
    days = max(1, min(days, 90))
    since = day_start_utc(days - 1)
    return {"days": days, "items": admin_repo.usage_by_day(db, since)}


# ---------------------------------------------------------------- 排障：思考链
@router.get("/admin/conversations/{public_id}/reasoning")
def conversation_reasoning(
    public_id: str, db: DbSession, _admin: CurrentAdmin, limit: int = 20
) -> dict[str, Any]:
    """某会话最近若干条**思考过程**。只给后台看 —— 它会复述上下文，
    等于半个记忆，绝不能开给普通客户端（见 docs/09）。"""
    conversation = admin_repo.find_conversation(db, public_id)
    if conversation is None:
        raise errors.no_conversation()

    limit = max(1, min(limit, 100))
    rows = admin_repo.messages_with_reasoning(db, conversation.id, limit=limit)
    return {
        "conversation_id": conversation.public_id,
        "items": [
            {
                "seq": row.seq,
                "speaker": row.speaker,
                "reply": row.text[:500],
                "reasoning": row.reasoning or "",
                "created_at": to_iso_str(row.created_at),
            }
            for row in rows
        ],
    }


__all__ = ["router"]
