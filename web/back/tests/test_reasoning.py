"""tests/test_reasoning.py —— 思考过程只存服务端，绝不下发。

项目书（docs/08 §1.3）原话：「思考过程不对用户开放，只作为服务器的存储用，
只存在服务器上。」所以这里守两个方向：

  1. 存进去了 —— `messages.reasoning` 有值
  2. 漏不出去 —— 消息接口的响应里找不到它，正文里也没有它
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import keys
from app.db import SessionLocal
from app.models import Conversation, Message
from app.settings import get_settings
from app.upstream import Reply

THINKING = "她刚才在犹豫，要不要说实话。先说一半，剩下的等他再问。"


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        return Reply(
            text="（把杯子放下）我在。",
            reasoning=THINKING if think else "",
            prompt_tokens=30,
            completion_tokens=8,
        )

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return {}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_reasoning_is_stored_when_think_on(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]

    resp = client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "think-1", "text": "你在想什么", "think": True},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.public_id == public_id))
        assert conversation is not None
        row = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.side == "assistant"
            )
        )
    assert row is not None
    assert row.reasoning == THINKING
    # 正文里不能混进思考链
    assert THINKING not in row.text


def test_reasoning_is_empty_when_think_off(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]
    client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "think-off", "text": "在吗"},
        headers=_headers(token),
    )
    with SessionLocal() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.public_id == public_id))
        assert conversation is not None
        row = db.scalar(
            select(Message).where(
                Message.conversation_id == conversation.id, Message.side == "assistant"
            )
        )
    assert row is not None
    assert not row.reasoning


def test_reasoning_never_leaks_to_client(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """最要命的一条：思考链绝不能出现在任何响应体里。"""
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]

    created = client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "leak-check", "text": "在吗", "think": True},
        headers=_headers(token),
    )
    assert THINKING not in created.text

    listing = client.get(
        f"/v1/conversations/{public_id}/messages", headers=_headers(token)
    )
    assert THINKING not in listing.text
    for item in listing.json()["items"]:
        assert "reasoning" not in item
