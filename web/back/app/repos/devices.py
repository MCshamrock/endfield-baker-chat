"""repos/devices.py —— 设备表的所有 SQL。

约定（整个 repos/ 目录都遵守）：
  * 函数第一个参数一定是数据库会话 `db`
  * 只做"查/写"，**不 commit** —— 什么时候提交由路由层决定，因为一次请求可能写多张表
  * 需要立刻被后续查询看到时，自己 `db.flush()`（本层允许，路由层不许）
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.ids import new_public_id, new_token, sha256_hex
from app.models import Device


def create(
    db: Session,
    *,
    platform: str,
    app_ver: str,
    app_build: int,
    model: str,
    os_ver: str,
    tz: str,
    locale: str,
) -> tuple[Device, str]:
    """注册一台新设备，返回 (设备, 明文 token)。

    明文 token 只在这里出现一次：调用方必须立刻返回给客户端，之后库里只有哈希。
    """
    token = new_token()
    now = utcnow()
    device = Device(
        public_id=new_public_id(),
        token_hash=sha256_hex(token),
        platform=platform,
        app_ver=app_ver,
        app_build=app_build,
        model=model,
        os_ver=os_ver,
        tz=tz,
        locale=locale,
        created_at=now,
        last_seen_at=now,
    )
    db.add(device)
    # flush 而不 commit：主键要马上拿到，但事务还没结束
    db.flush()
    return device, token


def find_by_token_hash(db: Session, token_hash: str) -> Device | None:
    """按 token 哈希找设备。找不到返回 None（由调用方决定回 401 还是别的）。"""
    return db.scalar(select(Device).where(Device.token_hash == token_hash))


def touch(db: Session, device: Device) -> None:
    """记一下"这台设备刚刚来过"。用于排障时判断用户还在不在用。"""
    device.last_seen_at = utcnow()
    db.flush()


def bind_owner(db: Session, device: Device, user_id: int | None) -> None:
    """把设备挂到账号上（登录）或摘下来（登出）。

    登录**不换 token**：客户端手里那个 token 继续用，只是它现在"有主"了。
    这样客户端不用保存两套凭证，也不需要"重新初始化"这一步。
    """
    device.owner_user_id = user_id
    db.flush()


def list_ids_for_owner(db: Session, user_id: int) -> list[int]:
    """这个账号名下所有设备的主键。跨设备可见就是靠它。"""
    stmt = select(Device.id).where(Device.owner_user_id == user_id).order_by(Device.id)
    return list(db.scalars(stmt).all())


def list_for_owner(db: Session, user_id: int) -> list[Device]:
    """这个账号名下的设备（给"我的设备"列表看）。"""
    stmt = select(Device).where(Device.owner_user_id == user_id).order_by(Device.id.desc())
    return list(db.scalars(stmt).all())


def unbind_others(db: Session, *, user_id: int, keep_device_id: int) -> int:
    """把这个账号名下**除当前设备以外**的设备全部摘掉，返回摘了几台。

    改密码时调用。效果等于别的站的"改密后所有会话失效"：
    别人拿着的旧手机立刻看不到这个账号的数据，自己这台不用重新登录。
    设备本身和它的 token 都还在，只是从账号上摘下来了 —— 数据一条没动。
    """
    stmt = select(Device).where(
        Device.owner_user_id == user_id,
        Device.id != keep_device_id,
    )
    rows = list(db.scalars(stmt).all())
    for device in rows:
        device.owner_user_id = None
    db.flush()
    return len(rows)
