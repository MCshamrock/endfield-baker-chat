"""clock.py —— 时间只有一种写法：UTC，而且不带时区。

为什么是"不带时区"的 UTC（naive UTC），而不是 `datetime.now(timezone.utc)` 那种带 tzinfo：
  MySQL 的 DATETIME 不存时区。带 tzinfo 的对象写进去、读出来会变成 naive，
  如果再拿它和 `now(tz)` 比大小，就会报 "can't compare offset-naive and offset-aware"；
  更阴的情况是它不报错、只是差了 8 小时。
  所以约定：**内存里一律 naive UTC，只在出接口的那一刻补上 +00:00。**

为什么全项目只留一个 `utcnow()`：
  代码里散落 `datetime.now()` 迟早会有人写出本地时间。全库一个入口，grep 一次就能查干净。
  验收清单里有一条就是 `grep -rn "datetime.now()" app/` 必须为空。
"""

from datetime import UTC, datetime, timedelta, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。写库、比较大小、算耗时都用它。"""
    return datetime.now(UTC).replace(tzinfo=None)


def to_iso(value: datetime | None) -> str | None:
    """把库里的 naive UTC 转成给人/给客户端看的 ISO8601（带 +00:00）。

    None 原样返回 None —— 调用方不用到处写 if。
    """
    if value is None:
        return None
    return to_iso_str(value)


def to_iso_str(value: datetime) -> str:
    """确定不为 None 的版本。字段是非空时用它，省得为了满足类型检查写一堆断言。"""
    return value.replace(tzinfo=UTC).isoformat()


# 站点的"日历日"按东八区切，不按 UTC（站长、用户都在东八区）。
# 固定 +8 就够：CST 没有夏令时，不需要 tzdata。
# 后台的"今日 / 按天"统计边界都由这一个数决定（见 day_start_utc、repos/admin.py）。
STATS_TZ_OFFSET_HOURS = 8

_CST = timezone(timedelta(hours=STATS_TZ_OFFSET_HOURS))
_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def local_time_text(value: datetime | None = None) -> str:
    """给提示词用的"现在几点"：东八区 + 时段词 + 24 小时制。

    为什么必须带时段词：只给 `03:15` 时模型经常按 12 小时制理解，
    把凌晨三点答成"下午三点"（2026-11-18 线上实测）。
    老契约 `/chat` 的客户端不传时间，所以服务端要能自己给出一个。
    """
    stamp = (value or utcnow()).replace(tzinfo=UTC).astimezone(_CST)
    hour = stamp.hour
    if hour < 5:
        period = "凌晨"
    elif hour < 9:
        period = "早上"
    elif hour < 11:
        period = "上午"
    elif hour < 13:
        period = "中午"
    elif hour < 18:
        period = "下午"
    elif hour < 23:
        period = "晚上"
    else:
        period = "深夜"
    return f"{stamp:%Y-%m-%d} {period}{stamp:%H:%M}（24小时制） {_WEEK[stamp.weekday()]}"


def day_start_utc(days_back: int = 0) -> datetime:
    """东八区"某一天零点"换算成 UTC 时刻（naive UTC，能直接和库里的 created_at 比大小）。

    为什么需要这个函数：库里一律存 naive UTC，`date(created_at)` 切出来的是 UTC 日；
    但人都在东八区，照 UTC 切天，东八区凌晨 0–8 点的请求会被算进"前一天"，
    站长看后台会觉得数字对不上自己的表。所以统计的日界改按东八区切。

    换算举例：东八区 11-20 00:00 = UTC 11-19 16:00。
    `days_back` 是往前数几天（0 = 今天，6 = 六天前）。

    注意：**只给"按天统计"用**。写库、比大小、算耗时仍然一律 naive UTC。
    """
    local_now = utcnow() + timedelta(hours=STATS_TZ_OFFSET_HOURS)
    local_start = (local_now - timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_start - timedelta(hours=STATS_TZ_OFFSET_HOURS)
