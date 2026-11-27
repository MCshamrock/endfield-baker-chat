"""repos/users.py —— 账号表的所有 SQL。

登录方式这一版只有一种：**用户名 + 密码**。

为什么不用邮箱：

  * 没有发信能力。邮箱的唯一用处就是"发验证码/找回密码"，这两件事现在都做不了，
    让用户填一个收不到信的邮箱只是多一步；
  * 用户是玩家，注册时让他为了一个账号去拼英文邮箱，没必要；
  * 表结构里 `login_type / login_id` 本来就是为多种登录方式留的，
    将来加邮箱/QQ 只是多一个 `login_type`，不用改表。

用户名的规则（和 `api/auth.py` 的校验、客户端的表单提示必须一致）：

    2–20 个字符，只能用中文 / 字母 / 数字 / 下划线；首位必须是中文或字母

**大小写不敏感**靠 MySQL 的 `utf8mb4_0900_ai_ci` 排序规则实现：`Alice` 和 `alice`
落在同一个唯一键值上。所以查询不用自己 `lower()`（那样反而会造出"库里存大小写、
查的时候小写"两套口径），存原样、比的时候由库负责。
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.ids import new_public_id
from app.models import User

# login_id 字段宽度（schema 里是 VARCHAR(190)，用户名本身只用到 20）
LOGIN_ID_MAX = 190
DISPLAY_NAME_MAX = 64

USERNAME_MIN = 2
USERNAME_MAX = 20
# 首位：中文 / 字母（下划线和数字都不许打头，否则看起来像内部标识）；
# 其余：再加数字和下划线
USERNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{1,19}$")


def normalize_username(raw: str) -> str:
    """去首尾空白。

    **不做大小写转换**：唯一性和查询都交给库的 `_ai_ci` 排序规则，
    存用户打的原样（"Alice" 显示出来还是 Alice），比对时自然不区分大小写。
    """
    return raw.strip()


def is_valid_username(name: str) -> bool:
    return bool(USERNAME_PATTERN.match(name))


def create(
    db: Session,
    *,
    login_id: str,
    pass_hash: str,
    display_name: str = "",
    login_type: str = "username",
) -> User:
    """建一个账号。调用方要保证 login_id 已经过 `normalize_username` + 格式校验。"""
    now = utcnow()
    user = User(
        public_id=new_public_id(),
        login_type=login_type,
        login_id=login_id,
        pass_hash=pass_hash,
        display_name=_display_name_of(display_name, login_id),
        status="active",
        created_at=now,
        last_seen_at=now,
    )
    db.add(user)
    db.flush()
    return user


def find_by_login_id(db: Session, login_id: str) -> User | None:
    """按用户名找账号（大小写由库的排序规则忽略）。找不到返回 None。"""
    return db.scalar(select(User).where(User.login_id == login_id))


def find_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def find_by_public_id(db: Session, public_id: str) -> User | None:
    return db.scalar(select(User).where(User.public_id == public_id))


def touch(db: Session, user: User) -> None:
    """记一下"这个账号刚来过"（判断僵尸账号要用）。"""
    user.last_seen_at = utcnow()
    db.flush()


def set_password(db: Session, user: User, pass_hash: str) -> None:
    user.pass_hash = pass_hash
    db.flush()


def _display_name_of(raw: str, login_id: str) -> str:
    """昵称：用户给了就用（截断到字段宽度），没给就默认等于用户名。

    昵称和用户名分开是为了两件事：用户名要唯一且字符集受限，昵称可以随便写
    （将来会进提示词，让人格叫得顺口）。
    """
    name = raw.strip()
    if not name:
        name = login_id
    return name[:DISPLAY_NAME_MAX]


__all__ = [
    "LOGIN_ID_MAX",
    "USERNAME_MAX",
    "USERNAME_MIN",
    "create",
    "find_by_id",
    "find_by_login_id",
    "find_by_public_id",
    "is_valid_username",
    "normalize_username",
    "set_password",
    "touch",
]
