"""在服务器上验收：后台"按天"统计的日界是不是真的按东八区切。

只读：不发写请求（用依赖覆盖假装管理员），不往库里插任何一行。

用法（在 ~/baker-v4/back 下，靠 .env 连生产库）：
    /home/y/baker-v4/venv/bin/python scripts/verify_day_boundary.py

注意：它连的是**当前 .env 指向的库**（prod 目录 = 生产库）。只读，不写任何一行。
"""

from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.clock import STATS_TZ_OFFSET_HOURS, day_start_utc, utcnow
from app.db import SessionLocal
from app.deps import current_admin
from app.main import app
from app.repos import admin as admin_repo


def main() -> None:
    start = day_start_utc()
    week_start = day_start_utc(6)
    local_start = start + timedelta(hours=STATS_TZ_OFFSET_HOURS)
    cst_today = str(local_start.date())
    utc_day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        repo_today = admin_repo.usage_summary(db, start)
        repo_utc_today = admin_repo.usage_summary(db, utc_day_start)
        manual = db.execute(
            text(
                "SELECT DATE(created_at + INTERVAL "
                f"{STATS_TZ_OFFSET_HOURS} HOUR) d, COUNT(*) c "
                "FROM usage_events WHERE created_at >= :s GROUP BY d ORDER BY d"
            ),
            {"s": week_start},
        ).all()
        manual_today = int(
            db.execute(
                text("SELECT COUNT(*) FROM usage_events WHERE created_at >= :s"),
                {"s": start},
            ).scalar_one()
        )

    # 假装管理员，走真实的 HTTP 路由（不写库）
    app.dependency_overrides[current_admin] = lambda: SimpleNamespace(id=1, role="admin")
    try:
        client = TestClient(app)
        overview = client.get("/v1/admin/overview")
        body = overview.json()
        usage7 = client.get("/v1/admin/usage?days=7").json()
    finally:
        app.dependency_overrides.clear()

    api_today = body["today"]["requests"]
    api_days = [(d["date"], d["requests"]) for d in body["days"]]
    sql_days = [(str(row[0]), int(row[1])) for row in manual]

    print(f"东八区今天零点 = UTC {start}   （= 东八区 {local_start}，日键 {cst_today}）")
    print(f"接口 HTTP {overview.status_code}")
    print(f"today.requests  接口={api_today}  SQL 手算={manual_today}  仓库={repo_today['requests']}")
    print(f"对照 · 同一批行按 UTC 切天，'今天'会算成 {repo_utc_today['requests']} 条"
          f"（多吞了 {repo_utc_today['requests'] - api_today} 条本该属于昨天的）")
    print(f"days（接口）={api_days}")
    print(f"days（SQL） ={sql_days}")
    print(f"usage?days=7 返回 {len(usage7['items'])} 天，days 字段={usage7['days']}")
    ok = (
        overview.status_code == 200
        and api_today == manual_today
        and api_days == sql_days
        and len(usage7["items"]) == len(sql_days)
    )
    print("结论：", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
