"""scripts/cleanup_uploads.py —— 清掉过期 / 领完的取件码包。

## 为什么需要它

取件码是"**免登录也能往服务器塞文件**"的入口。不清理的话，没人领走的包会一直
占着磁盘（全站 2GB 的额度就是被这类包吃掉的），而磁盘满了的表现是
"所有上传都回 507"——很难联想到是几个谁都没要的包。

它和 `POST /v1/uploads` 里那次"顺手清 20 条"用的是同一段逻辑
（`app/uploads.py: cleanup`），区别只是这里**不限条数**、由运维或定时任务跑。

## 用法

    # 看一眼有多少可清的（不删）
    .venv-linux/bin/python scripts/cleanup_uploads.py --dry-run

    # 真清
    .venv-linux/bin/python scripts/cleanup_uploads.py

    # 只清 100 条（怕一次删太久、把库锁住时用）
    .venv-linux/bin/python scripts/cleanup_uploads.py --limit 100

放 crontab 里每小时跑一次就够了（TTL 是 24 小时，不需要更勤）：

    17 * * * * cd /opt/baker/back && .venv/bin/python scripts/cleanup_uploads.py
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

# 让脚本能直接 `python scripts/xxx.py` 跑：把仓库根目录塞进 import 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import uploads  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.repos import uploads as uploads_repo  # noqa: E402
from app.settings import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="清理过期/领完的取件码包")
    parser.add_argument("--limit", type=int, default=0, help="最多清几条（0 = 不限）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删")
    args = parser.parse_args()

    limit = args.limit if args.limit > 0 else None
    settings = get_settings()

    with SessionLocal() as db:
        if args.dry_run:
            # dry-run 只查不删，所以直接问仓库层"有哪些该清"
            rows = uploads_repo.list_expired(
                db,
                now=utcnow(),
                ttl=timedelta(hours=settings.upload_ttl_hours),
                limit=limit,
            )
            total = sum(row.bytes for row in rows)
            print(f"可以清理 {len(rows)} 条，约 {total / (1024 * 1024):.1f} MB（dry-run，没删）")
            return 0

        removed = uploads.cleanup(db, limit=limit)
        db.commit()
        print(f"清理了 {removed} 条过期取件记录/临时文件")
        print(f"取件包目录：{settings.upload_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
