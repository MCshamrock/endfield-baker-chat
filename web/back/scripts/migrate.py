"""scripts/migrate.py —— 迁移的命令行入口。

逻辑都在 `app/migrate.py`，这里只负责：
  1. 决定连哪个库（默认取 .env 里的 BAKER_DATABASE_URL，也可以用 --database-url 覆盖）
  2. 打印结果，让人一眼看出"这次跑了几个文件"

为什么要命令行入口而不是"应用启动时自动建表"：
  自动建表会在生产上悄悄改结构，出问题根本不知道是哪次启动干的。
  迁移必须是**显式的、能看日志的**一步。
"""

import argparse
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，直接 print 中文会变成乱码。这里固定成 UTF-8。
# 有些环境会把 stdout 换成没有 reconfigure 的对象，所以先看一眼（hasattr 顺带做了类型收窄）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 允许 `python scripts/migrate.py` 直接跑（此时 sys.path 里只有 scripts/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.migrate import apply_all, list_migration_files  # noqa: E402
from app.settings import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="执行 schema/*.sql 里还没跑过的迁移")
    parser.add_argument(
        "--database-url",
        default=None,
        help="默认用 .env 的 BAKER_DATABASE_URL",
    )
    args = parser.parse_args()

    database_url = args.database_url or get_settings().database_url
    total = len(list_migration_files())
    applied = apply_all(database_url)

    if applied:
        print(f"执行了 {len(applied)} 个迁移：{', '.join(applied)}")
    else:
        print("没有新迁移要跑")
    print(f"schema 目录共 {total} 个文件；库：{_safe_url(database_url)}")
    return 0


def _safe_url(database_url: str) -> str:
    """打印库地址时把密码抹掉，免得贴日志时泄露。"""
    if "@" not in database_url:
        return database_url
    head, tail = database_url.split("@", 1)
    scheme, _, rest = head.partition("://")
    user = rest.split(":", 1)[0]
    return f"{scheme}://{user}:***@{tail}"


if __name__ == "__main__":
    raise SystemExit(main())
