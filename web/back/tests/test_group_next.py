"""tests/test_group_next.py —— 群聊「下一句」：这条链真的会推进。

这是"直到所有人发言完毕"的发动机。守四件事：
  1. 不传玩家消息也能再让一位说一句（这是之前缺的那块）
  2. 本轮说过的人不会再被抽到（无放回），池子空了就 `done=true`
  3. 重放同一个 client_msg_id 不会多出一条
  4. 单聊调它要 400（有别的事去 /continue）
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import keys
from app.db import SessionLocal
from app.settings import get_settings
from app.upstream import Reply

MEMBERS = ["perlica", "chenqianyu"]
NAMES = {"佩丽卡", "陈千语"}


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"count": 0}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        state["count"] += 1
        return Reply(text=f"（点头）第 {state['count']} 句。", prompt_tokens=20, completion_tokens=5)

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _group(client: TestClient, token: str, mode: str = "weighted") -> str:
    resp = client.post(
        "/v1/conversations",
        json={"mode": "group", "members": MEMBERS, "speak_mode": mode},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_next_advances_until_everyone_spoke(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = _group(client, token)

    first = client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "u-1", "text": "你们都在吗"},
        headers=_headers(token),
    ).json()
    spoke = {first["speaker"]}
    assert first["speaker"] in NAMES

    # 第二句：不用玩家再说话，靠 /next 推进
    second = client.post(
        f"/v1/conversations/{public_id}/next",
        json={"client_msg_id": "n-1"},
        headers=_headers(token),
    ).json()
    assert second["speaker"] in NAMES
    assert second["speaker"] not in spoke, "本轮说过的人不该再被抽到"
    spoke.add(second["speaker"])
    assert second["done"] is True  # 两个人一人一句，本轮结束

    # 第三句：池子空了，什么也不生成
    third = client.post(
        f"/v1/conversations/{public_id}/next",
        json={"client_msg_id": "n-2"},
        headers=_headers(token),
    ).json()
    assert third["reply"] is None and third["done"] is True

    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = "
                "(SELECT id FROM conversations WHERE public_id = :p)"
            ),
            {"p": public_id},
        ).scalar()
    assert rows == 3  # 用户 1 条 + 角色 2 条，第三次没有新增


def test_next_is_idempotent(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = _group(client, token)
    client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "u-2", "text": "在吗"},
        headers=_headers(token),
    )

    payload = {"client_msg_id": "same-next"}
    first = client.post(
        f"/v1/conversations/{public_id}/next", json=payload, headers=_headers(token)
    ).json()
    second = client.post(
        f"/v1/conversations/{public_id}/next", json=payload, headers=_headers(token)
    ).json()

    assert second["duplicate"] is True
    assert second["reply_seq"] == first["reply_seq"]
    assert second["speaker"] == first["speaker"]


def test_next_can_name_the_speaker(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """指定谁说：传 speaker 就听玩家的（slug 或中文名都认）。"""
    _device_id, token = device
    public_id = _group(client, token)
    client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "u-3", "text": "在吗"},
        headers=_headers(token),
    )
    body = client.post(
        f"/v1/conversations/{public_id}/next",
        json={"client_msg_id": "n-pick", "speaker": "chenqianyu"},
        headers=_headers(token),
    ).json()
    assert body["speaker"] == "陈千语"


def test_next_rejects_single_chat(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]
    resp = client.post(
        f"/v1/conversations/{public_id}/next",
        json={"client_msg_id": "n-single"},
        headers=_headers(token),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "not_group"
