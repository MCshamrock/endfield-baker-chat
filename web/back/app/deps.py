"""deps.py —— FastAPI 的依赖（一个请求进来，先过这几关）。

现在有三道：
  1. `get_db`：给本次请求一个数据库会话（定义在 db.py，这里只是转成类型别名）
  2. `current_device`：从 `Authorization: Bearer xxx` 认出是哪台设备
  3. `current_identity`：在设备之上再认"它属于哪个账号"（访客就没有）

为什么鉴权放在依赖里而不是写进每个函数：
  写在依赖里，路由函数只要声明一个 `identity: CurrentIdentity` 参数就有鉴权；
  忘了声明 = 拿不到身份 = 代码根本写不下去。比"每个函数开头自己判断一下"可靠得多。
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app import errors
from app.db import get_db
from app.ids import sha256_hex
from app.models import Device, User
from app.owners import owner_key
from app.repos import devices as devices_repo
from app.repos import users as users_repo

# 路由函数签名里写 `db: DbSession` 即可拿到会话
DbSession = Annotated[Session, Depends(get_db)]


def current_device(
    db: DbSession,
    authorization: Annotated[str, Header()] = "",
) -> Device:
    """从 Bearer token 认出设备；认不出就 401。

    错误码分两种（no_token / bad_token），客户端据此知道该提示"请先初始化"还是"请重新初始化"。
    对外文案故意含糊，不泄露"这个 token 到底存不存在"。
    """
    token = _extract_bearer(authorization)
    if not token:
        raise errors.no_token()

    device = devices_repo.find_by_token_hash(db, sha256_hex(token))
    if device is None:
        raise errors.bad_token()

    devices_repo.touch(db, device)
    return device


# 路由函数签名里写 `device: CurrentDevice` 就有了鉴权。
# 这个名字必须定义在 current_device 之后 —— 别名要引用它。
CurrentDevice = Annotated[Device, Depends(current_device)]


@dataclass(slots=True)
class Identity:
    """一次请求里的"我是谁"。**所有数据归属判断都应该用它，而不是直接看 device。**

    三种情况：

    * 访客：`user=None`，`device_ids=[本设备]` —— 只能看到自己这台设备的东西
    * 已登录：`user=<账号>`，`device_ids=<该账号名下所有设备>` —— 跨设备可见
    * 账号被停用：`user=None` —— 当访客处理（不 500，也不让人继续用别人的记录）

    `device` 永远有值：即使登录了，写消息时也要知道是哪台设备发出来的
    （排障、限流、附件归属都还要用到它）。
    """

    device: Device
    user: User | None
    device_ids: list[int]

    @property
    def user_id(self) -> int | None:
        """登录态返回账号主键，访客返回 None。仓库层按它决定查哪一片数据。"""
        return self.user.id if self.user is not None else None

    @property
    def owner_key(self) -> str:
        """数据归属键：`u:<user_id>` 或 `d:<device_id>`（干员记忆、发言权重用它）。"""
        return owner_key(self.user_id, self.device.id)


def current_identity(
    db: DbSession, device: CurrentDevice
) -> Identity:
    """设备 + 它所属的账号（如果有）。

    每请求多两条小查询（账号一行、名下设备几行），都是主键/索引查询。
    想省也可以把 device_ids 缓进 device 行，但"改一次登录状态就得清缓存"是
    另一类 bug 的来源 —— 这点开销不值得换。
    """
    if device.owner_user_id is None:
        return Identity(device=device, user=None, device_ids=[device.id])

    user = users_repo.find_by_id(db, device.owner_user_id)
    if user is None or user.status != "active":
        # 账号被删/被停用：按访客走。不报错 —— 用户手里那张 token 还是有效的，
        # 只是看不到账号里的数据了（重新登录会告诉他"账号不可用"）。
        return Identity(device=device, user=None, device_ids=[device.id])

    return Identity(
        device=device,
        user=user,
        device_ids=devices_repo.list_ids_for_owner(db, user.id),
    )


# 路由函数签名里写 `identity: CurrentIdentity`：既有鉴权，也有"我属于谁"。
CurrentIdentity = Annotated[Identity, Depends(current_identity)]


def current_admin(identity: CurrentIdentity) -> User:
    """管理员才放行。**看的是账号，不是设备** —— 换台设备登录同一账号，权限跟着走。

    403 而不是 404：后台路径是公开的（前端要知道打哪儿），
    藏起来只会让人以为是路径写错了。
    """
    if identity.user is None or identity.user.role != "admin":
        raise errors.forbidden()
    return identity.user


# 路由函数签名里写 `admin: CurrentAdmin` 就是"必须管理员"。
CurrentAdmin = Annotated[User, Depends(current_admin)]


def _extract_bearer(authorization: str) -> str:
    """从 `Bearer abc123` 里取出 abc123。格式不对就当作没带。"""
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return ""
    return authorization[len(prefix) :].strip()
