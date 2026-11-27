"""repos/characters.py —— 角色表的查询。

第一版只有两个函数：列全部、按 slug 找一个。
角色提示词（character_prompts）等第 2 刀做生成时再来读。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Character


def list_enabled(db: Session) -> list[Character]:
    """所有上架中的角色，按 sort_order 排。客户端拿这个替换掉它自己的硬编码表。"""
    stmt = (
        select(Character)
        .where(Character.enabled == 1)
        .order_by(Character.sort_order, Character.id)
    )
    return list(db.scalars(stmt).all())


def find_by_slug(db: Session, slug: str) -> Character | None:
    """按 slug 找角色。下架的角色也算"找不到"（对客户端来说它已经不存在了）。"""
    return db.scalar(
        select(Character).where(Character.slug == slug, Character.enabled == 1)
    )


def find_by_id(db: Session, character_id: int) -> Character | None:
    """按主键找角色（出话时要用它的 slug 和名字）。

    这里**不过滤 enabled**：会话已经建好了，就算角色后来下架，
    这个会话也得继续能聊 —— 否则用户的历史记录会突然变成"找不到人"。
    """
    return db.get(Character, character_id)


def default_weights(db: Session, character_ids: list[int]) -> dict[int, int]:
    """批量取角色自带的发言权重：`{character_id: active_weight}`。

    为什么专门一个函数：群聊选人、会话详情、老群聊兼容层都要"先拿默认值、
    再叠玩家覆盖"。一次 `IN (...)` 查完，别在循环里一个个 `db.get`。
    """
    if not character_ids:
        return {}
    rows = db.scalars(select(Character).where(Character.id.in_(character_ids)))
    return {row.id: row.active_weight for row in rows}


def optional_owner_key(db: Session, authorization: str) -> str | None:
    """**可选身份**：带了有效 token 就返回 owner_key，没有/无效就返回 None。

    和 `app/deps.py` 的 `current_identity` 的区别只有一点：**认不出来不报错**。
    因为用到它的两个地方都是"没登录也要能用"的：
      * `GET /v1/characters` —— 首页就要显示角色，不能逼人先注册；
      * 老群聊 `/group/reply` —— 老客户端根本不带 token。
    规则和正式鉴权保持一致：登录账号优先（`u:<id>`），没登录/账号停用就按设备
    （`d:<id>`）；认不出设备和账号就返回 None（= 按角色默认权重算）。

    ⚠️ 这个函数严格说该住在 `deps.py`，但本次任务不允许改那个文件，
    所以暂放在角色仓库里（它要查的也是"这个 token 对应谁"，和角色查询同层）。
    以后能改 `deps.py` 时，把这段搬过去复用，别留两份。
    """
    from app.ids import sha256_hex
    from app.owners import owner_key
    from app.repos import devices as devices_repo
    from app.repos import users as users_repo

    token = authorization.strip()
    prefix = "Bearer "
    if token.startswith(prefix):
        token = token[len(prefix) :].strip()
    if not token:
        return None

    device = devices_repo.find_by_token_hash(db, sha256_hex(token))
    if device is None:
        return None

    user_id = device.owner_user_id
    if user_id is not None:
        user = users_repo.find_by_id(db, user_id)
        # 账号被删/停用时和正式鉴权一样，降级成访客（按设备算）
        if user is None or user.status != "active":
            user_id = None
    return owner_key(user_id, device.id)
