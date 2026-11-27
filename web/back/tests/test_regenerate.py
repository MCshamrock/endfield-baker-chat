"""tests/test_regenerate.py —— 「重新生成」：重写这一轮的回话，绝不新增玩家消息。

线上事故的现场：点一次「重新生成」，服务端会话里就多出一条一模一样的玩家消息，
下一次拼提示词时模型看到玩家把同一句话说了两遍 ——
"我明明只叫了她一声，她说我叫了两次"。

这个文件守的就是那条不变量：**无论点多少次重新生成，玩家消息的条数都不变**。
顺带守住三个分支：更早的某一轮（库一动不动）、库里没有这句（按正常发送处理）、
群聊（明确拒绝，群聊重生成走群聊那一轮）。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """记录每次送给上游的 messages —— 断言"那句玩家话只出现一次"就靠它。"""
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
        return Reply(
            text=f"（把杯子放下）第 {state['count']} 次回答。",
            prompt_tokens=30,
            completion_tokens=8,
        )

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """流式那一路的假上游（网页默认走 SSE）。"""
    state: dict[str, Any] = {"count": 0, "seen": []}

    async def _stream(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ):
        state["count"] += 1
        state["seen"].append(messages)
        for chunk in ("（把杯子放下）", "流式回的第 ", f"{state['count']} 句。"):
            yield chunk

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.stream", _stream)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _conversation(client: TestClient, token: str, *, group: bool = False) -> str:
    body: dict[str, Any] = {"mode": "group", "members": ["perlica", "chenqianyu"]}
    if not group:
        body = {"character_id": "perlica"}
    resp = client.post("/v1/conversations", json=body, headers=_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _say(client: TestClient, token: str, conversation_id: str, text_: str, cid: str) -> dict[str, Any]:
    resp = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": cid, "text": text_},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _regen(
    client: TestClient, token: str, conversation_id: str, text_: str, cid: str
) -> dict[str, Any]:
    resp = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": cid, "text": text_, "regenerate": True},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(client: TestClient, token: str, conversation_id: str) -> list[dict[str, Any]]:
    resp = client.get(
        f"/v1/conversations/{conversation_id}/messages", headers=_headers(token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def test_regenerate_replaces_reply_in_place(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "叫一声我的名字", "c-1")
    before = _rows(client, token, conversation_id)
    assert [row["side"] for row in before] == ["user", "assistant"]

    body = _regen(client, token, conversation_id, "叫一声我的名字", "c-regen-1")
    after = _rows(client, token, conversation_id)

    # 1) 玩家消息没有变多：还是「玩家 + 角色」两条
    assert [row["side"] for row in after] == ["user", "assistant"]
    # 2) 回话落回原来的位置（seq 不变），内容确实换了一份
    assert after[1]["seq"] == before[1]["seq"]
    assert after[1]["text"] != before[1]["text"]
    assert body["reply"] == after[1]["text"]
    assert body["reply_seq"] == before[1]["seq"]


def test_regenerate_sends_the_player_line_once(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """病根就在这句：历史里出现两句一样的玩家话，模型才会说"你叫了我两次"。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "在吗", "c-2")
    _regen(client, token, conversation_id, "在吗", "c-regen-2")

    seen = fake_upstream["seen"][-1]
    hits = [m for m in seen if m.get("role") == "user" and m.get("content") == "在吗"]
    assert len(hits) == 1, f"上行提示词里出现了 {len(hits)} 次「在吗」：{seen}"


def test_regenerate_last_reply_twice_keeps_two_rows(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """连点两次：每次都是"替换"，不是"追加"。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "摸摸头", "c-3")
    _regen(client, token, conversation_id, "摸摸头", "c-regen-3a")
    _regen(client, token, conversation_id, "摸摸头", "c-regen-3b")

    rows = _rows(client, token, conversation_id)
    assert [row["side"] for row in rows] == ["user", "assistant"]
    assert fake_upstream["count"] == 3


def test_regenerate_older_turn_does_not_touch_storage(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """重生成更早的一轮：库一动不动（删掉后面几轮是真丢数据）。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "第一句", "c-4")
    _say(client, token, conversation_id, "第二句", "c-5")
    before = _rows(client, token, conversation_id)
    assert len(before) == 4

    body = _regen(client, token, conversation_id, "第一句", "c-regen-4")
    after = _rows(client, token, conversation_id)

    # 库里一行都没变，但客户端拿到了新回话
    assert after == before
    assert body["reply"]
    # 生成时用的历史只到"第一句"为止 —— 第二句不该进提示词
    seen = fake_upstream["seen"][-1]
    assert not any(m.get("content") == "第二句" for m in seen)


def test_regenerate_unknown_text_falls_back_to_normal_send(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """服务端根本没收到过这句（例如刚绑定会话）：按正常发消息补上，别丢。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _regen(client, token, conversation_id, "第一次见面", "c-regen-5")

    rows = _rows(client, token, conversation_id)
    assert [row["side"] for row in rows] == ["user", "assistant"]
    assert rows[0]["text"] == "第一次见面"


def test_regenerate_rejects_group_conversation(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    conversation_id = _conversation(client, token, group=True)
    resp = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": "c-regen-6", "text": "大家好", "regenerate": True},
        headers=_headers(token),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "regenerate_unsupported"


def test_stream_regenerate_does_not_add_user_message(
    client: TestClient, device: tuple[str, str], fake_stream: dict[str, Any]
) -> None:
    """网页默认走 SSE：流式那一路也必须"只换回话"。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)

    def _stream_post(**extra: Any):
        return client.post(
            f"/v1/conversations/{conversation_id}/messages:stream",
            json={"client_msg_id": "c-7", "text": "喂", **extra},
            headers=_headers(token),
        )

    first = _stream_post()
    assert first.status_code == 200, first.text
    assert "event: done" in first.text
    before = _rows(client, token, conversation_id)
    assert [row["side"] for row in before] == ["user", "assistant"]

    second = _stream_post(client_msg_id="c-regen-7", regenerate=True)
    assert second.status_code == 200, second.text
    assert "event: done" in second.text
    after = _rows(client, token, conversation_id)

    assert [row["side"] for row in after] == ["user", "assistant"]
    assert after[0]["seq"] == before[0]["seq"]
    assert after[1]["seq"] == before[1]["seq"]
    assert after[1]["text"] != before[1]["text"]
