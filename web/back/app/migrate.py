"""migrate.py —— 按文件名顺序执行 `schema/*.sql`，并记录执行过哪些。

为什么不用 Alembic：
  这个项目只有一个部署目标（一台服务器）、一共 13 张表。Alembic 自动生成的迁移文件
  会把"改一个字段"写成十几行 alter 描述，读的人不敢动；手写 SQL 谁都看得懂。

代价（写清楚，别装看不见）：
  1. **没有 downgrade**。回滚靠备份，不靠 migrate。所以每次改表前先 dump 一份。
  2. **执行过的文件不能改**。改了对已部署的库不生效（这里只按文件名判断跑没跑过）。
     要改表就新建 `003_xxx.sql`，绝不回头改 `001_init.sql`。

版本表 `schema_versions` 由本模块自己建 —— 它属于"迁移系统"，不属于业务表，
所以不放在 001_init.sql 里。
"""

from pathlib import Path

import pymysql
from pymysql.constants import CLIENT
from sqlalchemy.engine import make_url

# schema 目录：本文件在 <back>/app/ 下，往上两层就是 web/back/
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# 建版本表。第一次跑 migrate 时若不存在就建。
_VERSION_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version    VARCHAR(120) NOT NULL COMMENT '迁移文件名（不带 .sql）',
    applied_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='已经执行过的迁移文件。按文件名记录，顺序即执行顺序'
"""


def list_migration_files() -> list[Path]:
    """按文件名排序返回 schema/*.sql（001 → 002 → …）。排序就是执行顺序。"""
    return sorted(SCHEMA_DIR.glob("*.sql"))


def applied_versions(database_url: str) -> list[str]:
    """查库：已经执行过哪些迁移。"""
    conn = _connect(database_url)
    try:
        with conn.cursor() as cursor:
            cursor.execute(_VERSION_TABLE_DDL)
            cursor.execute("SELECT version FROM schema_versions ORDER BY version")
            return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def apply_all(database_url: str) -> list[str]:
    """把还没执行过的迁移依次跑掉，返回本次执行的文件名列表。

    一个文件 = 一个事务。失败就整个文件回滚，不会留下"跑了一半"的表结构。
    """
    conn = _connect(database_url)
    done: list[str] = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(_VERSION_TABLE_DDL)
            cursor.execute("SELECT version FROM schema_versions")
            already = {row[0] for row in cursor.fetchall()}
        conn.commit()

        for path in list_migration_files():
            version = path.stem
            if version in already:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cursor:
                cursor.execute(sql)
                # 一个文件里有多条语句（MULTI_STATEMENTS），要把结果集一个个走完，
                # 否则下一条 execute 会报 "Commands out of sync"
                while cursor.nextset():
                    pass
                cursor.execute(
                    "INSERT INTO schema_versions (version) VALUES (%s)", (version,)
                )
            conn.commit()
            done.append(version)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return done


def _connect(database_url: str) -> pymysql.connections.Connection:
    """把 SQLAlchemy 形式的 URL 拆成 pymysql 连接参数。

    复用同一份 URL 字符串（而不是再写一遍 host/port/密码），
    是为了保证 migrate 和服务端连的一定是同一个库。
    """
    url = make_url(database_url)
    return pymysql.connect(
        host=url.host or "127.0.0.1",
        port=url.port or 3306,
        user=url.username or "root",
        password=url.password or "",
        database=url.database,
        charset="utf8mb4",
        # 允许一次 execute 里有多条 ; 分隔的语句（我们的 .sql 文件就是一整份）
        client_flag=CLIENT.MULTI_STATEMENTS,
        # 时间同样钉成 UTC，和 app/db.py 保持一致
        init_command="SET time_zone = '+00:00'",
        autocommit=False,
    )
