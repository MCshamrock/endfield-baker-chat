"""models/base.py —— ORM 的公共底座：Base 类 + 几个类型别名。

为什么要类型别名：
  `Mapped[int] = mapped_column(UInt64, ...)` 一眼能看出"64 位无符号"，比写一长串
  `BIGINT(unsigned=True)` 好读，也比 `BigInteger` 安全（后者默认有符号，和 DDL 不一致）。

这些别名必须和 `schema/*.sql` 里的类型一一对应。对不上的话 `tests/test_schema.py` 会挂 ——
那张测试就是专门防"改了 DDL 忘了改模型"或者反过来。
"""

from sqlalchemy.dialects.mysql import (
    BIGINT,
    CHAR,
    DATETIME,
    INTEGER,
    MEDIUMTEXT,
    TINYINT,
    VARBINARY,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有表的基类。SQLAlchemy 2.x 的写法，不用再写 declarative_base()。"""


# ---- 字段类型别名（全部对齐 schema/001_init.sql）----
UInt64 = BIGINT(unsigned=True)          # 主键、seq、token 计数
UInt32 = INTEGER(unsigned=True)         # 普通外键（和主键同宽，避免隐式转换）
FixedChar32 = CHAR(32)                  # public_id
UtcDateTime = DATETIME(fsp=6)           # 时间：一律 UTC，精确到微秒
LongText = MEDIUMTEXT                   # 消息正文、人设（16MB）
SmallFlag = TINYINT(display_width=1)    # 布尔
Blob512 = VARBINARY(512)                # 密钥密文
