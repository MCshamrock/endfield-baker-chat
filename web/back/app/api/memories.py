"""api/memories.py —— 干员记忆：看、删、置顶、开关，以及发言权重。

五个端点，全都按 `identity.owner_key` 划边界（别人的记忆永远看不到，也删不到）。

这一组端点的存在意义不只是"管理"：**记忆必须能被看见、能被删掉**。
用户投诉"她记错了""我不想让她记这个"时，如果只能重置会话，那这功能就废了。
"""

from fastapi import APIRouter

from app import errors
from app.clock import to_iso_str
from app.deps import CurrentIdentity, DbSession
from app.repos import characters as characters_repo
from app.repos import memories as memories_repo
from app.schemas import (
    MemoryListOut,
    MemoryOut,
    MemoryPatchRequest,
    MemorySettingsOut,
    MemorySettingsRequest,
    WeightOut,
    WeightRequest,
)

router = APIRouter(tags=["memories"])


@router.get("/memories", response_model=MemoryListOut)
def list_memories(character_id: str, db: DbSession, identity: CurrentIdentity) -> MemoryListOut:
    """某个干员记着你什么。`mode` 是这一层的实际开关（off 时账本还在，只是不注入）。"""
    character = characters_repo.find_by_slug(db, character_id)
    if character is None:
        raise errors.no_character()

    rows = memories_repo.list_active(
        db, owner_key=identity.owner_key, character_id=character.id
    )
    mode = memories_repo.get_mode(db, identity.owner_key)
    return MemoryListOut(
        character_id=character.slug,
        mode=mode,
        items=[
            MemoryOut(
                id=row.id,
                character_id=character.slug,
                kind=row.kind,
                text=row.text,
                pinned=bool(row.pinned),
                created_at=to_iso_str(row.created_at),
                updated_at=to_iso_str(row.updated_at),
            )
            for row in rows
        ],
        total=len(rows),
        limit=memories_repo.MAX_ENTRIES,
    )


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: int, db: DbSession, identity: CurrentIdentity) -> None:
    """删一条。软删 + 留指纹：下次提炼不会再把它写回来。"""
    ok = memories_repo.remove(db, owner_key=identity.owner_key, memory_id=memory_id)
    if not ok:
        raise errors.AppError("no_memory", "没有这条记忆", http_status=404)
    db.commit()


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def patch_memory(
    memory_id: int,
    req: MemoryPatchRequest,
    db: DbSession,
    identity: CurrentIdentity,
) -> MemoryOut:
    """改文本 / 置顶。改文本会重算指纹——它已经是一条不同的记忆了。"""
    row = memories_repo.patch(
        db,
        owner_key=identity.owner_key,
        memory_id=memory_id,
        text=req.text,
        pinned=req.pinned,
    )
    if row is None:
        raise errors.AppError("no_memory", "没有这条记忆", http_status=404)
    db.commit()
    return MemoryOut(
        id=row.id,
        character_id="",
        kind=row.kind,
        text=row.text,
        pinned=bool(row.pinned),
        created_at=to_iso_str(row.created_at),
        updated_at=to_iso_str(row.updated_at),
    )


@router.get("/memories/settings", response_model=MemorySettingsOut)
def get_settings_(db: DbSession, identity: CurrentIdentity) -> MemorySettingsOut:
    return MemorySettingsOut(mode=memories_repo.get_mode(db, identity.owner_key))


@router.put("/memories/settings", response_model=MemorySettingsOut)
def put_settings_(
    req: MemorySettingsRequest, db: DbSession, identity: CurrentIdentity
) -> MemorySettingsOut:
    mode = memories_repo.set_mode(db, identity.owner_key, req.mode)
    db.commit()
    return MemorySettingsOut(mode=mode)


@router.put("/characters/{slug}/weight", response_model=WeightOut)
def put_weight(
    slug: str, req: WeightRequest, db: DbSession, identity: CurrentIdentity
) -> WeightOut:
    """调她在群里的发言权重（1~100）。只影响这个玩家自己的群聊。"""
    character = characters_repo.find_by_slug(db, slug)
    if character is None:
        raise errors.no_character()
    value = memories_repo.set_weight(
        db, owner_key=identity.owner_key, character_id=character.id, active=req.active
    )
    db.commit()
    return WeightOut(character_id=character.slug, active=value, source="user")


@router.delete("/characters/{slug}/weight", response_model=WeightOut)
def delete_weight(slug: str, db: DbSession, identity: CurrentIdentity) -> WeightOut:
    """恢复默认权重：删掉这个玩家的覆盖行，之后按角色自带的 `active_weight` 算。

    返回 `{character_id, active: <角色默认值>, source: "default"}`。

    **幂等**：本来就没覆盖过也回 200（一样是 default）。客户端点「恢复默认」
    不该因为"你本来就没改过"而报错 —— 用户根本分不清这两种状态。
    """
    character = characters_repo.find_by_slug(db, slug)
    if character is None:
        raise errors.no_character()
    memories_repo.delete_weight(
        db, owner_key=identity.owner_key, character_id=character.id
    )
    db.commit()
    return WeightOut(
        character_id=character.slug,
        active=character.active_weight,
        source="default",
    )

