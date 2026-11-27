"""db.py —— 数据库连接。全部参数都是踩过坑之后才这么写的。

两个关键设置，别顺手改掉：

1. `init_command="SET time_zone = '+00:00'"`
   每个连接建立时都把会话时区钉成 UTC。否则 MySQL 会用服务器时区解释 `NOW()`、
   也会在 TIMESTAMP 上做隐式转换，代码里的时间和库里的时间就悄悄差 8 小时。

2. `autoflush=False`
   自动 flush 会在你查询时"顺手"把未提交的对象写进去，行为很难预测（原型上踩过：
   刚 add 的用户消息查不到，把上一条当成了当前消息）。
   所以规则是：**要查之前手动 `flush()`**，其余时间不让 ORM 自作主张。

连接池参数：
   `pool_pre_ping=True` —— 从池里拿连接前先探一下，避免拿到被 MySQL 单方面掐断的死连接
   （默认 8 小时空闲断开，服务端跑一晚上第二天第一个请求就会 500，这是最常见的"隔夜崩"）。
   `pool_recycle=3600` —— 一小时强制换新连接，比 MySQL 的 wait_timeout 更早，双保险。
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.settings import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
    connect_args={
        "charset": "utf8mb4",
        "init_command": "SET time_zone = '+00:00'",
    },
)

# expire_on_commit=False：提交后对象还能读字段。
# 默认是 True（提交后再访问属性会重新查库），在"提交完接着组装响应"的场景里纯属浪费一次查询。
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session，请求结束一定关掉。

    这里**不自动提交**。提交由业务函数显式调用 `db.commit()`，
    因为"什么算一个事务"必须由写业务的人决定，不能让框架猜。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
