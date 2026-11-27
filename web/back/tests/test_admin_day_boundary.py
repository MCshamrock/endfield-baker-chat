"""tests/test_admin_day_boundary.py —— 后台"按天"统计的日界。

一条规矩：**日界按东八区切，不按 UTC**（2026-11-20 改；此前是 UTC 日，
东八区凌晨 0–8 点的请求会被算进"前一天"，站长看后台对不上自己的表）。

守四件事：
  1. 东八区零点换算回 UTC 是对的（+8 的减法人人能写反，所以正着验一遍）；
  2. `days_back` 真的往前挪整天，不是挪 23 小时；
  3. 库里 15:59 UTC 与 16:00 UTC 两行，落在**两个不同的东八区日**；
  4. "今日"的起点真能把上一东八区日的那一行挡在外面。
"""

from datetime import datetime, timedelta

from app.clock import STATS_TZ_OFFSET_HOURS, day_start_utc, utcnow
from app.db import SessionLocal
from app.models import UsageEvent
from app.repos import admin as admin_repo


def test_day_start_is_cst_midnight() -> None:
    start = day_start_utc()
    # 换算回东八区，必须正好是某个零点
    local = start + timedelta(hours=STATS_TZ_OFFSET_HOURS)
    assert (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)
    # 而且"现在"一定落在这天之内（不是昨天、也不是明天）
    assert start <= utcnow() < start + timedelta(hours=24)


def test_days_back_moves_whole_days() -> None:
    assert day_start_utc(0) - day_start_utc(1) == timedelta(days=1)
    assert day_start_utc(6) == day_start_utc() - timedelta(days=6)


def _event(at: datetime) -> UsageEvent:
    return UsageEvent(
        request_id="pytest-day",
        model="pytest",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        first_token_ms=0,
        total_ms=10,
        status="ok",
        created_at=at,
    )


def test_utc_1559_and_1600_fall_on_different_cst_days() -> None:
    """15:59 UTC = 东八区当天 23:59；16:00 UTC = 东八区次日 00:00。"""
    with SessionLocal() as db:
        db.add_all(
            [_event(datetime(2026, 1, 1, 15, 59)), _event(datetime(2026, 1, 1, 16, 0))]
        )
        db.commit()
        rows = admin_repo.usage_by_day(db, datetime(2026, 1, 1, 0, 0))

    by_day = {row["date"]: row["requests"] for row in rows}
    assert by_day == {"2026-01-01": 1, "2026-01-02": 1}


def test_today_boundary_excludes_previous_cst_day() -> None:
    today = day_start_utc()
    with SessionLocal() as db:
        db.add_all([_event(today - timedelta(seconds=1)), _event(today)])
        db.commit()
        summary = admin_repo.usage_summary(db, today)

    assert summary["requests"] == 1
