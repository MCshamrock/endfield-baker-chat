"""api/auth.py —— 身份：设备（访客）+ 账号（登录）。

    POST /v1/auth/device     注册一台匿名设备（唯一不需要 token 的端点）
    POST /v1/auth/register   用户名 + 密码注册，并把当前设备挂上去
    POST /v1/auth/login      登录，并把当前设备挂上去
    POST /v1/auth/logout     退出账号（设备留在原地，数据还在账号里）
    GET  /v1/auth/me         我现在是谁（访客 / 哪个账号 / 名下几台设备）
    POST /v1/auth/password   改密码（会踢掉其它设备）

## 四个决定，都在这里体现

1. **登录不换 token。** 客户端手里那台设备的 token 继续用，登录只是给它挂一个
   `owner_user_id`。好处：客户端只有一套凭证，没有"登录后要重新初始化"这一步，
   老代码全都不用动。

2. **登录 = 把设备上的记录划到账号名下（改一列）。** 见
   `conversations_repo.adopt_owned`。不搬数据、不复制、不合并 —— 所以不会出现
   "两份数据打架"。

3. **登出不删数据。** 只是把设备从账号上摘下来。那些记录的 `user_id` 还在，
   重新登录就能看见；在别人手上时（共用手机）看不见。

4. **登录标识是用户名，不是邮箱。** 没有发信能力，邮箱的唯一用处（验证码 /
   找回密码）现在都用不上；用户名 + 密码是这个阶段最省事的形态。
"""

import logging
import secrets

from fastapi import APIRouter
from pydantic import Field
from sqlalchemy.orm import Session

from app import errors, gates, passwords, ratelimit, uploads
from app.api.uploads import _import_to_out
from app.clock import to_iso, to_iso_str
from app.deps import CurrentDevice, CurrentIdentity, DbSession
from app.models import Device, ImportBatch, User
from app.repos import conversations as conversations_repo
from app.repos import devices as devices_repo
from app.repos import users as users_repo
from app.schemas import (
    AuthResultOut,
    ChangePasswordRequest,
    DeviceOut,
    DeviceRegisteredOut,
    DeviceRegisterRequest,
    ImportResultOut,
    LoginRequest,
    MeOut,
    OwnerDataOut,
    RegisterRequest,
    UserOut,
)

logger = logging.getLogger("baker.auth")

router = APIRouter(tags=["auth"])

# 登录时"账号不存在"分支拿它做一次真正的哈希校验，让两条失败路径耗时一致。
# 不这么做的话，响应快慢本身就是一个"这个用户名注册过没有"的探测口。
_DUMMY_HASH = passwords.hash_password(secrets.token_hex(16))


def _clip(value: str, limit: int) -> str:
    """按数据库列宽截断设备元信息。

    为什么截断而不是报错：`os_ver` 这类字段是客户端顺手塞进来的 UA / 版本串，
    真实值经常超过 32 字。直接 422 会让"注册设备"整条链路挂掉（新前端拿不到 token，
    迁移、聊天全跟着失败）；而截断只损失一段没人看的元信息。
    """
    return (value or "")[:limit]


class RegisterWithClaimRequest(RegisterRequest):
    """注册请求 + 一个可选的取件码。

    `claim_code` 是"免登录先上传数据包"那张码（见 /v1/uploads）。注册时填上它，
    数据就会直接落到这个新账号名下，用户不用"先注册、再登录、再选一次文件"。

    为什么不写进 schemas.py 的 RegisterRequest：登录不需要这个字段，
    而且这里给它加了一个约束以外的语义（填了就会触发一次导入）。挂在注册
    专用的子类上，读代码的人一眼知道"只有注册会带码"。
    """

    claim_code: str = Field(default="", max_length=32)


class RegisterWithClaimOut(AuthResultOut):
    """注册结果。

    `import_result` 只有带了 `claim_code` 才有值 —— 它是那次领取的导入结果，
    形状和 `POST /v1/imports` 完全一样（卡片/会话/消息各几条、有没有重复）。
    客户端可以用它直接显示"已经从你的数据包里恢复 N 条对话"。
    """

    import_result: ImportResultOut | None = None


@router.post("/auth/device", response_model=DeviceRegisteredOut, status_code=201)
def register_device(req: DeviceRegisterRequest, db: DbSession) -> DeviceRegisteredOut:
    """注册一台设备，返回 (device_id, token)。

    `token` 只在这里出现一次 —— 客户端丢了就只能重新注册（拿到一台"新设备"，
    旧数据因为归属旧设备而看不到）。**登录可以救回来**：用同一账号登录一次，
    新设备就能看到账号名下的全部记录。
    """
    device, token = devices_repo.create(
        db,
        platform=_clip(req.platform, 32),
        app_ver=_clip(req.app_ver, 32),
        app_build=req.app_build,
        model=_clip(req.model, 64),
        os_ver=_clip(req.os_ver, 32),
        tz=_clip(req.tz, 64),
        locale=_clip(req.locale, 16),
    )
    db.commit()
    return DeviceRegisteredOut(device_id=device.public_id, token=token)


# ---------------------------------------------------------------- 注册 / 登录
@router.post("/auth/register", response_model=RegisterWithClaimOut, status_code=201)
def register(
    req: RegisterWithClaimRequest, db: DbSession, device: CurrentDevice
) -> RegisterWithClaimOut:
    """注册账号。注册成功即登录（当前设备挂上去）。

    `claim_code` 可选：带上它就在**同一条事务**里把那个数据包导到这个新账号名下。

    为什么必须同一个事务：如果"先注册、再导入"，导入这一步炸了用户就得到
    "账号建好了、数据没进来"的半截状态 —— 他会以为数据丢了，然后重传一次，
    结果库里多出一份重复数据。所以码不对就整次注册回滚，等于什么都没发生。
    """
    username = _clean_username(req.username)
    _check_password(req.password)
    claim_code = req.claim_code.strip()

    if users_repo.find_by_login_id(db, username) is not None:
        raise errors.username_taken()

    with gates.guarded(gates.scrypt_gate()):
        pass_hash = passwords.hash_password(req.password)

    user = users_repo.create(
        db,
        login_id=username,
        pass_hash=pass_hash,
        display_name=req.display_name,
    )
    adopted = _attach_device(db, device, user)

    imported: ImportResultOut | None = None
    if claim_code:
        batch, duplicate = _claim_in_register(db, claim_code, device, user)
        imported = _import_to_out(batch, duplicate=duplicate)

    db.commit()
    logger.info(
        "注册 %s 并绑定设备 %s，收编 %d 条会话，取件码=%s",
        user.public_id,
        device.public_id,
        adopted,
        claim_code or "(没填)",
    )
    return RegisterWithClaimOut(
        user=_user_out(user),
        adopted_conversations=adopted,
        import_result=imported,
    )


@router.post("/auth/login", response_model=AuthResultOut)
def login(req: LoginRequest, db: DbSession, device: CurrentDevice) -> AuthResultOut:
    """登录。成功后当前设备挂到账号上，账号下所有设备的历史都看得见。"""
    username = _clean_username(req.username)
    _throttle(device, username)

    user = users_repo.find_by_login_id(db, username)
    with gates.guarded(gates.scrypt_gate()):
        if user is None:
            # 走一遍真哈希，让"账号不存在"和"密码错"两条路径的耗时一样（见 _DUMMY_HASH）
            passwords.verify_password(req.password, _DUMMY_HASH)
            raise errors.bad_credentials()
        if not passwords.verify_password(req.password, user.pass_hash):
            raise errors.bad_credentials()

    if user.status != "active":
        raise errors.AppError("account_disabled", "这个账号已被停用", http_status=403)

    adopted = _attach_device(db, device, user)
    users_repo.touch(db, user)
    # 试对了就把失败计数归零：否则白天误输两次、晚上再错一次会被当成"连续错三次"
    ratelimit.login_by_account().clear(f"u{username}")
    db.commit()
    logger.info("登录 %s 设备 %s，收编 %d 条会话", user.public_id, device.public_id, adopted)
    return AuthResultOut(user=_user_out(user), adopted_conversations=adopted)


@router.post("/auth/logout", status_code=204)
def logout(db: DbSession, identity: CurrentIdentity) -> None:
    """退出账号。**不删任何数据**，只是把设备从账号上摘下来。"""
    devices_repo.bind_owner(db, identity.device, None)
    db.commit()


@router.get("/auth/me", response_model=MeOut)
def me(db: DbSession, identity: CurrentIdentity) -> MeOut:
    """我现在是谁。客户端启动时调一次，决定显示"登录"还是"我的账号"。"""
    if identity.user is None:
        return MeOut(kind="guest")

    rows = devices_repo.list_for_owner(db, identity.user.id)
    return MeOut(
        kind="user",
        user=_user_out(identity.user),
        devices=[_device_out(row) for row in rows],
    )


@router.get("/auth/owner-data", response_model=OwnerDataOut)
def owner_data(db: DbSession, identity: CurrentIdentity) -> OwnerDataOut:
    """这份身份名下有没有聊天记录（只回数量）。

    客户端启动时调一次，用来回答一个很具体的问题：
    **这个人是不是已经有记录、但还没登录账号？**

    为什么要问：现在的记录可能正挂在"这台设备"名下（`d:<device_id>`）。
    设备凭证存在浏览器里，清一次缓存、换一台设备，那批记录就跟丢了 ——
    用户看到的就是"我的聊天记录全没了"。所以在有记录时先把人拦下来登录/注册，
    登录会把设备名下的记录改挂到账号（`adopt_owned`，只改一列，不搬数据）。

    已登录账号直接回账号名下的数量（登录后这个数一般只会涨）。
    """
    conversations, messages = conversations_repo.count_for_owner(
        db, user_id=identity.user_id, device_id=identity.device.id
    )
    return OwnerDataOut(conversations=conversations, messages=messages)


@router.post("/auth/password", status_code=204)
def change_password(
    req: ChangePasswordRequest, db: DbSession, identity: CurrentIdentity
) -> None:
    """改密码。必须验旧密码 —— 否则手机被人拿走就能改掉密码把人锁在外面。"""
    user = identity.user
    if user is None:
        raise errors.no_user()

    with gates.guarded(gates.scrypt_gate()):
        if not passwords.verify_password(req.old_password, user.pass_hash):
            raise errors.bad_credentials()
        _check_password(req.new_password)
        new_hash = passwords.hash_password(req.new_password)

    users_repo.set_password(db, user, new_hash)
    # 改完密码把其它设备全部摘掉 —— 这是"改密踢下线"在本架构里的等价物：
    # 别人手里的旧设备立刻看不到这个账号的数据，自己这台不用重新登录。
    dropped = devices_repo.unbind_others(
        db, user_id=user.id, keep_device_id=identity.device.id
    )
    db.commit()
    logger.info("账号 %s 改了密码，摘掉其它 %d 台设备", user.public_id, dropped)


# ---------------------------------------------------------------- 内部
def _attach_device(db: Session, device: Device, user: User) -> int:
    """把设备挂到账号上，并把设备上还没归属的会话划过来，返回划了几条。

    这台设备如果已经挂着**另一个**账号，直接拒：那是"共用手机换号"的场景，
    悄悄改挂会把前一个人的记录露给后一个人。要换号必须先登出。
    """
    if device.owner_user_id is not None and device.owner_user_id != user.id:
        raise errors.device_taken()

    devices_repo.bind_owner(db, device, user.id)
    return conversations_repo.adopt_owned(db, device_id=device.id, user_id=user.id)


def _claim_in_register(
    db: Session, code: str, device: Device, user: User
) -> tuple[ImportBatch, bool]:
    """在注册这条事务里领取取件码并导入，返回 (导入批次, 是否重复)。

    码不能用（不存在 / 过期 / 文件丢了 / 刚好被别人清掉）就**先把整次注册撤掉**，
    再报 400 `bad_claim_code`。

    为什么显式 `rollback()`：账号和设备绑定已经 flush 进这条事务了。只 raise 的话，
    要等请求结束、session 关掉才会回滚 —— 那段时间里事务里还留着"半个账号"，
    谁在这段代码后面再加一句查询，就可能读到不该存在的用户。
    显式回滚把"整次注册回滚"这件事写在代码里，而不是靠框架的默认行为。
    """
    try:
        return uploads.claim(db, code=code, device=device, user_id=user.id)
    except uploads.ClaimCodeError as exc:
        db.rollback()
        raise errors.AppError(
            "bad_claim_code",
            "取件码无效或已经过期，请重新上传一次数据包再注册",
            http_status=400,
        ) from exc


def _throttle(device: Device, username: str) -> None:
    """登录尝试限流：每设备 + 每账号，两道都要过。

    账号那道的 key 用小写：`Alice` 和 `alice` 是同一个账号，
    否则换个大小写就换了个桶，限流白做。
    """
    allowed, retry = ratelimit.login_by_device().allow(f"d{device.id}")
    if not allowed:
        raise errors.too_many_attempts(retry)
    allowed, retry = ratelimit.login_by_account().allow(f"u{username.lower()}")
    if not allowed:
        raise errors.too_many_attempts(retry)


def _clean_username(raw: str) -> str:
    """用户名归一化 + 格式校验。规则写在一处（users_repo），前端提示照抄它。"""
    name = users_repo.normalize_username(raw)
    if not users_repo.is_valid_username(name):
        raise errors.bad_request(
            f"用户名要 {users_repo.USERNAME_MIN}–{users_repo.USERNAME_MAX} 个字符，"
            "只能用中文、字母、数字、下划线，且必须以中文或字母开头"
        )
    return name


def _check_password(raw: str) -> None:
    if not passwords.is_acceptable(raw):
        raise errors.weak_password(
            f"密码长度要在 {passwords.MIN_PASSWORD_LENGTH}–"
            f"{passwords.MAX_PASSWORD_LENGTH} 个字符之间"
        )


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.public_id,
        username=user.login_id or "",
        display_name=user.display_name,
        created_at=to_iso_str(user.created_at),
    )


def _device_out(device: Device) -> DeviceOut:
    return DeviceOut(
        id=device.public_id,
        platform=device.platform,
        app_ver=device.app_ver,
        last_seen_at=to_iso(device.last_seen_at),
    )


__all__ = ["router"]
