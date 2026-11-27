"""models/identity.py —— 身份两张表：users（账号）、devices（设备）。

真正在用的是 devices：用户不登录也能有完整数据，归属靠设备 token。
users 的 `owner_user_id` 一旦填上，数据就挂到账号名下 ——
这就是"访客转登录"不需要搬运数据的原因。

登录标识是**用户名**（`login_type='username'`），不是邮箱：这一版没有发信能力，
让用户填一个收不到验证信的邮箱只是多一步。将来要加邮箱/QQ 登录，
`login_type` 已经留好位置，唯一键 `uk_users_login_id (login_type, login_id)`
也不用改。
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, FixedChar32, UInt64, UtcDateTime


class User(Base):
    """账号。第一版不启用登录。"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_users_public_id"),
        # MySQL 唯一索引允许多个 NULL，所以一堆访客（login_id 为 NULL）不会互相撞
        UniqueConstraint("login_type", "login_id", name="uk_users_login_id"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(FixedChar32, nullable=False)
    login_type: Mapped[str] = mapped_column(
        ENUM("none", "username", "email", "qq"), nullable=False, default="none"
    )
    login_id: Mapped[str | None] = mapped_column(String(190), nullable=True)
    pass_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        ENUM("active", "disabled"), nullable=False, default="active"
    )
    # 后台权限。默认 user —— 第一个管理员用 scripts/make_admin.py 手工指定
    role: Mapped[str] = mapped_column(
        ENUM("user", "admin"), nullable=False, default="user"
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Device(Base):
    """设备。匿名身份，token 只在注册那一次返回明文。"""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("public_id", name="uk_devices_public_id"),
        UniqueConstraint("token_hash", name="uk_devices_token_hash"),
        Index("ix_devices_owner", "owner_user_id"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(FixedChar32, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(
        UInt64, ForeignKey("users.id", name="fk_devices_user"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    app_ver: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    app_build: Mapped[int] = mapped_column(nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    os_ver: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    tz: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


__all__ = ["Device", "User"]
