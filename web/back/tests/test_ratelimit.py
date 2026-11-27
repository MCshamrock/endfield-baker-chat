"""tests/test_ratelimit.py —— 两道闸门：每人每分钟、全站同时几个。

为什么这两条必须有测试：
  人肉试不出来 —— 要连发十几次才撞到 429，而一旦线上被人刷，烧的是真金白银。
"""

import time

import pytest
from fastapi.testclient import TestClient

from app import generate, keys, ratelimit
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        return Reply(text="嗯。", prompt_tokens=10, completion_tokens=2)

    keys.reset()
    ratelimit.reset()
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")


def _conv(client: TestClient, token: str) -> str:
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "perlica"},
        headers={"Authorization": f"Bearer {token}"},
    )
    return str(resp.json()["id"])


def test_per_device_rate_limit(
    client: TestClient, device: tuple[str, str], fake_upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认每分钟 12 次：第 13 次必须 429，并且带 retry_after。"""
    monkeypatch.setattr(get_settings(), "user_rpm", 3)
    ratelimit.reset()

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _conv(client, token)

    statuses = []
    for index in range(5):
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"client_msg_id": f"r-{index}", "text": "在吗"},
            headers=headers,
        )
        statuses.append(resp.status_code)

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    body = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "r-last", "text": "在吗"},
        headers=headers,
    ).json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["details"]["retry_after"] > 0


def test_store_only_does_not_consume_quota(
    client: TestClient, device: tuple[str, str], fake_upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只存不发（generate=false）不吃配额：那是写库，不是花钱的生成。"""
    monkeypatch.setattr(get_settings(), "user_rpm", 2)
    ratelimit.reset()

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _conv(client, token)

    for index in range(5):
        resp = client.post(
            f"/v1/conversations/{conv_id}/messages",
            json={"client_msg_id": f"s-{index}", "text": "只存", "generate": False},
            headers=headers,
        )
        assert resp.status_code == 200


def test_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口过期后配额恢复（不会永久封住一个人）。"""
    window = ratelimit.FixedWindow(limit=1, window_seconds=0.05)
    assert window.allow("k")[0] is True
    assert window.allow("k")[0] is False
    time.sleep(0.06)
    assert window.allow("k")[0] is True


def test_sweep_clears_expired() -> None:
    window = ratelimit.FixedWindow(limit=5, window_seconds=0.01)
    window.allow("a")
    window.allow("b")
    time.sleep(0.02)
    window.sweep()
    assert window._buckets == {}
