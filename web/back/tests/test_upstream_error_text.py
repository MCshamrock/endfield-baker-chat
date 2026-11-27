"""上游错误的文案（upstream._error_text）。

这一条是 2026-11-18 的用户可见缺陷：流式分支把上游的原始 JSON 直接拼进气泡，
长这样 ——

    上游 HTTP 429: {"error":{"message":"今日文本额度已用完，当前处于限速模式：
    每 3 分钟可请求 1 次，请于 23:05:11 后重试。 (request id: 2026...)"}}

用户看到的应该是一句人话，JSON 留给日志。所以这里钉住三件事：
  1. 429 且能解析出时刻 → 带「请于 HH:MM 后再试」；
  2. 429 但解析不出时刻 → 退回「上游限速（429），请稍等再发」；
  3. 其它状态码 → 保留状态码 + 截断后的原文（够定位，不刷屏）。
"""

import httpx

from app.upstream import _error_text, _retry_clock


def _response(status: int, text: str) -> httpx.Response:
    return httpx.Response(status, text=text)


# 这是上游真给过的一段（2026-11-18 23:0x，限额用尽时）
_REAL_429 = (
    '{"error":{"message":"今日文本额度已用完，当前处于限速模式：每 3 分钟可请求 1 次，'
    '请于 23:05:11 后重试。 (request id: 202611181503148919453423cNKaxDA)",'
    '"type":"AgnesAI_error","param":"","code":"rate_limit_exceeded"}}'
)


def test_429_with_clock_is_readable():
    text = _error_text(_response(429, _REAL_429))
    assert "23:05" in text
    assert "额度" in text
    # 绝不能把 JSON 甩给用户
    assert "{" not in text and "request id" not in text


def test_429_without_clock_still_human():
    text = _error_text(_response(429, "too many requests"))
    assert text == "上游限速（429），请稍等再发"


def test_streaming_body_param_is_used():
    """流式分支传的是刚读出来的 body（response.text 在流式里拿不到）。"""
    text = _error_text(_response(429, ""), _REAL_429)
    assert "23:05" in text


def test_other_status_keeps_code_and_truncates():
    text = _error_text(_response(502, "bad gateway\nplease retry"))
    assert text.startswith("上游 HTTP 502：")
    assert "\n" not in text


def test_retry_clock_rejects_bad_hour():
    assert _retry_clock("请于 99:05:11 后重试") == ""
    assert _retry_clock("请于 07:05:11 后重试") == "07:05"
    assert _retry_clock("") == ""
