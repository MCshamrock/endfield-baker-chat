"""tests/test_sampling.py —— 全站采样参数：temp=1.3 / top_p=0.7。

2026-11-22 站长口径：`temp=1.3，topP=0.7` → 「全部模型输入按照这个设置」。

这个文件守三件事：
  1. 默认拼出来的请求体就是 1.3 / 0.7（普通聊天、重新生成、流式共用同一个 _build_payload）；
  2. 调用方显式传 temperature 时仍然以调用方为准（摘要 0.2、群聊主持判定 0.3 那两处不能被改）；
  3. `BAKER_UPSTREAM_TOP_P=0` 等于"不发这个字段、用上游默认"，而不是 top_p=0。
"""

import pytest

from app.keys import Credential
from app.settings import get_settings
from app.upstream import _build_payload


def _credential() -> Credential:
    return Credential(key="sk-test-0123456789", base_url="https://example.com/v1", model="m")


def _messages() -> list[dict[str, object]]:
    return [{"role": "user", "content": "在吗"}]


def test_default_payload_uses_1_3_and_0_7() -> None:
    payload = _build_payload(_credential(), _messages(), think=False, stream=False)
    assert payload["temperature"] == 1.3
    assert payload["top_p"] == 0.7


def test_stream_payload_also_uses_them() -> None:
    """流式（网页默认走的那个）用的也是同一个 _build_payload。"""
    payload = _build_payload(_credential(), _messages(), think=True, stream=True)
    assert payload["temperature"] == 1.3
    assert payload["top_p"] == 0.7


def test_caller_temperature_still_wins() -> None:
    """摘要/压缩与群聊主持那两处显式传了温度，不能被全站默认值带跑。"""
    payload = _build_payload(_credential(), _messages(), think=False, stream=False, temperature=0.2)
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.7


def test_top_p_override_changes_more_than_temperature() -> None:
    payload = _build_payload(_credential(), _messages(), think=False, stream=False, top_p=0.95)
    assert payload["top_p"] == 0.95


def test_top_p_zero_means_use_upstream_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 的语义是"别发这个字段" —— 发成 0 会让模型只在最大概率那个词里挑，别说。"""
    monkeypatch.setattr(get_settings(), "upstream_top_p", 0.0)
    payload = _build_payload(_credential(), _messages(), think=False, stream=False)
    assert "top_p" not in payload


def test_temperature_can_be_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """站长改口味只改 .env（BAKER_UPSTREAM_TEMPERATURE / BAKER_UPSTREAM_TOP_P），不改代码。"""
    monkeypatch.setattr(get_settings(), "upstream_temperature", 0.8)
    monkeypatch.setattr(get_settings(), "upstream_top_p", 0.9)
    payload = _build_payload(_credential(), _messages(), think=False, stream=False)
    assert payload["temperature"] == 0.8
    assert payload["top_p"] == 0.9
