"""tests/test_continue.py —— 「继续说」：不用玩家再打字，让角色接着往下说。

守四件事：
  1. 生成的是**一条新的 assistant 消息**，seq 递增，不产生空白的用户气泡
  2. 幂等：同一个 client_msg_id 重放不会多出第二段（网络重试是常事）
  3. 提示词里真的带上了「继续」指令 —— 不带的话模型会复读或总结
  4. 群聊明确拒绝（多一个"谁接"的分支，见 docs/07），而不是悄悄按单聊处理
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import keys
from app.db import SessionLocal
from app.prompt.rules import CONTINUE_RULE
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """记录每次请求的 messages，方便断言"继续"指令真的进去了。"""
    state: dict[str, Any] = {"count": 0, "seen": []}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        state["count"] += 1
        state["seen"].append(messages)
        return Reply(text=f"（把杯子放下）第 {state['count']} 段。", prompt_tokens=30, completion_tokens=8)

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _conversation(client: TestClient, token: str) -> str:
    resp = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _say(client: TestClient, token: str, conversation_id: str, text_: str) -> dict[str, Any]:
    resp = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": f"c-{text_}", "text": text_},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_continue_appends_a_new_reply(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token)
    first = _say(client, token, conversation_id, "在忙吗")

    resp = client.post(
        f"/v1/conversations/{conversation_id}/continue",
        json={"client_msg_id": "cont-1"},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"]
    assert body["reply_seq"] == first["reply_seq"] + 1
    assert body["duplicate"] is False

    # 历史里应该只有"用户一条 + 角色两条"，没有空白的用户消息
    msgs = client.get(
        f"/v1/conversations/{conversation_id}/messages", headers=_headers(token)
    ).json()["items"]
    sides = [item["side"] for item in msgs]
    assert sides == ["user", "assistant", "assistant"]


def test_continue_is_idempotent(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "在吗")

    payload = {"client_msg_id": "same-id"}
    first = client.post(
        f"/v1/conversations/{conversation_id}/continue", json=payload, headers=_headers(token)
    ).json()
    second = client.post(
        f"/v1/conversations/{conversation_id}/continue", json=payload, headers=_headers(token)
    ).json()

    assert second["duplicate"] is True
    assert second["reply_seq"] == first["reply_seq"]
    with SessionLocal() as db:
        total = db.execute(
            text("SELECT COUNT(*) FROM messages WHERE conversation_id = (SELECT id FROM conversations WHERE public_id = :p)"),
            {"p": conversation_id},
        ).scalar()
    assert total == 3  # 用户 1 条 + 角色 2 条，重放没有多出来


def test_continue_injects_the_rule(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "说说你今天干了什么")

    client.post(
        f"/v1/conversations/{conversation_id}/continue",
        json={"client_msg_id": "cont-rule"},
        headers=_headers(token),
    )
    last_messages = fake_upstream["seen"][-1]
    assert str(last_messages[-1]["content"]) == CONTINUE_RULE
    assert last_messages[-1]["role"] == "user"


def test_continue_without_history_is_rejected(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token)
    resp = client.post(
        f"/v1/conversations/{conversation_id}/continue",
        json={"client_msg_id": "cont-empty"},
        headers=_headers(token),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "nothing_to_continue"


def test_continue_is_rejected_in_group(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    resp = client.post(
        "/v1/conversations",
        json={"mode": "group", "members": ["perlica", "chenqianyu"], "speak_mode": "round"},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    conversation_id = resp.json()["id"]

    out = client.post(
        f"/v1/conversations/{conversation_id}/continue",
        json={"client_msg_id": "cont-group"},
        headers=_headers(token),
    )
    assert out.status_code == 400
    assert out.json()["error"]["code"] == "continue_unsupported"
