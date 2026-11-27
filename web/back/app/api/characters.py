"""api/characters.py —— `GET /v1/characters`：服务端权威的角色表。

有了这个端点，客户端就不该再硬编码"35 个角色 + 每个角色的 id"。
改角色、上下架、调顺序，全在服务端改，客户端不用发版。

这个端点**不需要 token**（首页就要显示角色）。但如果客户端带了 token，
就顺手把"这个玩家自己的有效权重"一起算好返回 —— 这样客户端不用再猜
"本地存过没有"，`weight` + `weight_source` 一眼看出该显示什么。
"""

from typing import Annotated

from fastapi import APIRouter, Header

from app.deps import DbSession
from app.repos import characters as characters_repo
from app.repos import memories as memories_repo
from app.schemas import CharacterListOut, CharacterOut

router = APIRouter(tags=["characters"])


@router.get("/characters", response_model=CharacterListOut)
def list_characters(
    db: DbSession,
    authorization: Annotated[str, Header()] = "",
) -> CharacterListOut:
    """列出所有上架角色。这个端点不需要 token —— 首页就要显示，别逼人先注册。

    每个角色回三个权重字段：

    * `active_weight` —— 角色**自带**的默认值，永远不变（老客户端只认它）
    * `weight`        —— **有效值**：玩家覆盖 ?? 角色默认
    * `weight_source` —— `"user"`（玩家改过）/ `"default"`（用角色自带值）

    没带 token 或 token 失效时，`weight` 就等于 `active_weight`、来源一律 default。
    这是故意的：公开接口不该因为"你的登录过期了"就变成 401。
    """
    rows = characters_repo.list_enabled(db)
    defaults = {row.id: row.active_weight for row in rows}
    # 可选身份：认得出是谁，就去 character_weights 查这个玩家的覆盖值
    owner = characters_repo.optional_owner_key(db, authorization)
    effective = memories_repo.effective_weights(db, owner_key=owner, defaults=defaults)

    items: list[CharacterOut] = []
    for row in rows:
        weight, source = effective[row.id]
        items.append(
            CharacterOut(
                id=row.slug,
                name=row.name,
                avatar=row.avatar,
                attribute=row.attribute,
                active_weight=row.active_weight,
                weight=weight,
                weight_source=source,
            )
        )
    return CharacterListOut(items=items)
