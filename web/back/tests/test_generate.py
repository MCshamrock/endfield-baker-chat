"""tests/test_generate.py —— 出话链路（用假上游，不花一分钱、不打外网）。

守四件事：
  1. 回复真的存进库了，而且顺序紧接在用户消息后面
  2. 用量落到 usage_events（cached_tokens 也要进来，它决定"粘性有没有生效"）
  3. 重发同一个 client_msg_id **不会重复生成**（否则用户会看到两遍同一句话）
  4. 上游挂了，用户那条消息仍然在库里（不能因为模型没回就把用户的话弄丢）
"""

import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import generate, keys
from app.db import SessionLocal
from app.settings import get_settings
from app.upstream import Reply, UpstreamError


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """把上游换成一个固定回复的假函数，并统计被调了几次。"""
    calls = {"count": 0}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        calls["count"] += 1
        return Reply(
            text="(点头)我在呢,你说。",
            prompt_tokens=120,
            completion_tokens=12,
            cached_tokens=96,
        )

    keys.reset()  # 池子有 10 秒缓存，测试之间必须清
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return calls


def _new_conversation(client: TestClient, token: str) -> str:
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "perlica"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_reply_is_generated_and_stored(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, int]
) -> None:
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _new_conversation(client, token)

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-1", "text": "在吗", "immersive": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["reply"] == "(点头)我在呢,你说。"
    assert body["reply_seq"] == 2  # 用户消息是 1，回复是 2
    assert body["usage"]["cached_tokens"] == 96

    listed = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers).json()
    assert [item["side"] for item in listed["items"]] == ["user", "assistant"]
    assert listed["items"][1]["text"] == body["reply"]


def test_usage_is_recorded(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, int]
) -> None:
    _device_id, token = device
    conv_id = _new_conversation(client, token)
    client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-usage", "text": "算一下账"},
        headers={"Authorization": f"Bearer {token}"},
    )

    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT prompt_tokens, completion_tokens, cached_tokens, status "
                "FROM usage_events ORDER BY id DESC LIMIT 1"
            )
        ).first()
    assert row is not None
    assert tuple(row) == (120, 12, 96, "ok")


def test_duplicate_does_not_generate_twice(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, int]
) -> None:
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _new_conversation(client, token)
    payload = {"client_msg_id": "c-dup", "text": "只发一次"}

    first = client.post(f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
    assert first.json()["duplicate"] is False
    assert fake_upstream["count"] == 1

    again = client.post(f"/v1/conversations/{conv_id}/messages", json=payload, headers=headers)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["reply"] is None
    assert fake_upstream["count"] == 1  # 没有第二次调用


def test_upstream_failure_keeps_user_message(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        raise UpstreamError("连不上上游：模拟故障")

    monkeypatch.setattr(generate.upstream, "complete", _boom)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _new_conversation(client, token)

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-fail", "text": "这句话不能丢"},
        headers=headers,
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"

    listed = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers).json()
    assert [item["side"] for item in listed["items"]] == ["user"]
    assert listed["items"][0]["text"] == "这句话不能丢"


def test_stream_delivers_filtered_text_and_stores_reply(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stream(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> AsyncIterator[str]:
        # 括号被故意拆成两段：第一段只有左括号，第二段才闭合
        yield "(轻轻"
        yield "叹气)我在。"
        yield "[sns_emoji_012]"

    monkeypatch.setattr(generate.upstream, "stream", _stream)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _new_conversation(client, token)

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages:stream",
        json={"client_msg_id": "c-stream", "text": "流式来一句", "immersive": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [block for block in resp.text.split("\n\n") if block.strip()]
    events = [block.split("\n")[0].removeprefix("event: ") for block in frames]
    # 第一段只有左括号、被整段吃掉；第三段是表情，翻译成（疑惑）后也被沉浸式过滤掉
    assert events == ["delta", "done"]

    text_parts = [
        json.loads(block.split("\n")[1].removeprefix("data: "))["text"] for block in frames[:-1]
    ]
    assert "".join(text_parts) == "我在。"

    listed = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers).json()
    assert listed["items"][-1]["side"] == "assistant"
    assert listed["items"][-1]["text"] == "我在。"


def test_stream_rejected_in_gateway_mode(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "gen_mode", "gateway")
    _device_id, token = device
    conv_id = _new_conversation(client, token)

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages:stream",
        json={"client_msg_id": "c-gw", "text": "你好"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_stream_in_gateway_mode"
