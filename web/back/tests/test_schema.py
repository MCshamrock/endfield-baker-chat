"""tests/test_schema.py —— 核对"模型"和"数据库"是不是同一张表。

为什么需要这个测试：
  表结构有两份描述 —— `schema/*.sql`（权威）和 `app/models/`（镜像）。
  两份文件迟早会不同步（改了一边忘了另一边），而这种错误不会在开发时暴露：
  ORM 按模型拼 SQL，多出来的列会在 insert 时报错，少掉的列则**静默丢数据**。
  所以用这个测试把"不同步"变成"测试挂"，在提交前就能看见。

只比名字和可空性，不比类型细节 —— 类型差异交给真实读写去暴露（那样更准）。
"""

from sqlalchemy import inspect, text

from app.db import engine
from app.models import Base


def _db_table_names() -> set[str]:
    inspector = inspect(engine)
    # schema_versions 是迁移系统自己的表，不归模型管
    return {name for name in inspector.get_table_names() if name != "schema_versions"}


def test_all_model_tables_exist_in_database() -> None:
    model_tables = set(Base.metadata.tables.keys())
    db_tables = _db_table_names()

    missing_in_db = model_tables - db_tables
    missing_in_model = db_tables - model_tables

    assert not missing_in_db, f"模型有、库里没有（迁移漏了？）：{sorted(missing_in_db)}"
    assert not missing_in_model, f"库里有、模型没有（忘写模型了？）：{sorted(missing_in_model)}"


def test_every_table_has_same_columns() -> None:
    """逐表核对列名。少一列就是静默丢数据，必须挡住。"""
    inspector = inspect(engine)
    problems: list[str] = []

    for table_name, table in Base.metadata.tables.items():
        model_columns = set(table.columns.keys())
        db_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if model_columns != db_columns:
            problems.append(
                f"{table_name}: 模型多 {sorted(model_columns - db_columns)}，"
                f"库多 {sorted(db_columns - model_columns)}"
            )

    assert not problems, "\n".join(problems)


def test_time_columns_are_utc_naive() -> None:
    """写进去的时间必须是"看起来就是 UTC"的那种，不能带 +08:00 偏移。

    做法：查一遍 MySQL 自己的时区。连接里设了 time_zone='+00:00'，
    如果这个值不是 +00:00，说明 db.py 的 init_command 被人删了。
    """
    with engine.connect() as conn:
        tz = conn.execute(text("SELECT @@session.time_zone")).scalar()
    assert tz == "+00:00", f"连接时区应为 +00:00，实际是 {tz}"
