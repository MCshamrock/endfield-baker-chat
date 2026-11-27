"""scripts/make_admin.py —— 把某个账号提成管理员（或者收回权限）。

为什么手工做：后台权限不能自己长出来。让"第一个注册的人自动是管理员"
是这类系统最常见的被抢号事故。

用法：
    .venv\\Scripts\\python.exe scripts\\make_admin.py --username 三叶草
    .venv\\Scripts\\python.exe scripts\\make_admin.py --username 三叶草 --revoke
    .venv\\Scripts\\python.exe scripts\\make_admin.py --list
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="管理后台权限")
    parser.add_argument("--username", default="", help="账号的用户名")
    parser.add_argument("--revoke", action="store_true", help="收回管理员权限")
    parser.add_argument("--list", action="store_true", help="列出当前所有管理员")
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.list:
            rows = list(db.scalars(select(User).where(User.role == "admin")))
            if not rows:
                print("还没有管理员。用 --username 指定一个。")
            for row in rows:
                print(f"  {row.login_id or '(无用户名)'}  status={row.status}  id={row.public_id}")
            return 0

        if not args.username:
            parser.error("要么 --username，要么 --list")

        user = db.scalar(select(User).where(User.login_id == args.username.strip()))
        if user is None:
            print(f"没有这个账号：{args.username}")
            return 2
        if user.status != "active":
            print(f"这个账号是 {user.status} 状态，先启用再提权")
            return 2

        user.role = "user" if args.revoke else "admin"
        db.commit()
        action = "已收回权限" if args.revoke else "已设为管理员"
        print(f"{action}：{user.login_id}（{user.public_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
