"""tests/test_client_echo.py —— 老客户端点「重新生成」＝把玩家那句话再发一遍。

2026-11-22 群里两条反馈：

  * 「现在的重新生成几乎没有效果……我重新生成，仍然是接着他刚才的话」
  * 「重新生成了几次后，他会说为什么会说好几遍」

4.0 之前的客户端（已装在用户手机/电脑上的那一版）点重新生成，走的是
"把玩家那句话再发一遍"：服务端收到的和"玩家又说了一遍"一模一样，于是
① 又插一条一样的玩家消息；② 那句"聊炸了"的回话还留在历史里，新回复继续接着它编。

这个文件守的就是那条兼容规则（`_plan_client_echo`）：
**正文与最后一条玩家消息一字不差、且那条是刚刚发的 → 按重新生成处理。**
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """记录每次送给上游的 messages —— "还接着上一句编"就靠它抓。"""
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


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _conversation(client: TestClient, token: str, *, group: bool = False) -> str:
    body: dict[str, Any] = (
        {"mode": "group", "members": ["perlica", "chenqianyu"]}
        if group
        else {"character_id": "perlica"}
    )
    resp = client.post("/v1/conversations", json=body, headers=_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _say(
    client: TestClient,
    token: str,
    conversation_id: str,
    text: str,
    cid: str,
    *,
    generate: bool = True,
) -> dict[str, Any]:
    resp = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": cid, "text": text, "generate": generate},
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


def test_old_client_resend_rewrites_the_turn(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """老客户端连点两次「重新生成」：玩家消息不增加，旧回话被换掉。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)

    _say(client, token, conversation_id, "叫一声我的名字", "c-old-1")
    before = _rows(client, token, conversation_id)
    assert [row["side"] for row in before] == ["user", "assistant"]

    # 老客户端的"重新生成"：同样的正文、新的 client_msg_id、不带 regenerate 标记
    body = _say(client, token, conversation_id, "叫一声我的名字", "c-old-regen-1")
    after = _rows(client, token, conversation_id)

    assert [row["side"] for row in after] == ["user", "assistant"]
    assert after[0]["seq"] == before[0]["seq"]
    assert after[1]["seq"] == before[1]["seq"]
    assert after[1]["text"] != before[1]["text"]
    assert body["reply"] == after[1]["text"]
    assert body["reply_seq"] == before[1]["seq"]
    assert body["duplicate"] is False

    # 再点一次也一样（群里"点了几次之后她说我说了好几遍"就是这条没守住）
    _say(client, token, conversation_id, "叫一声我的名字", "c-old-regen-1b")
    again = _rows(client, token, conversation_id)
    assert [row["side"] for row in again] == ["user", "assistant"]


def test_old_client_regenerate_does_not_feed_previous_reply(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """「重新生成几乎没有效果」的正因：旧回话还在提示词里，模型当然接着它编。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)

    first = _say(client, token, conversation_id, "我们去看海吧", "c-old-2")
    bad_reply = first["reply"]
    assert bad_reply

    _say(client, token, conversation_id, "我们去看海吧", "c-old-regen-2")

    seen = fake_upstream["seen"][-1]
    contents = [str(m.get("content") or "") for m in seen]
    assert not any(bad_reply in text for text in contents), "上一版回话还留在提示词里"
    hits = [text for text in contents if text.startswith("我们去看海吧")]
    assert len(hits) == 1, f"玩家这句出现了 {len(hits)} 次"


def test_seed_import_echo_does_not_duplicate_the_line(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """种子里混进了"正要发的这一句"：导入把它写成一行，紧接着又发一遍。

    网页与客户端都出现过（导入的包以玩家这句结尾）—— 现场是两条一模一样的玩家消息。
    """
    _device_id, token = device
    conversation_id = _conversation(client, token)

    # 先只存不发：等价于"种子导入把这句写进了库"
    _say(client, token, conversation_id, "早上好呀", "c-seed-1", generate=False)
    assert [row["side"] for row in _rows(client, token, conversation_id)] == ["user"]

    # 紧接着真的发同一条
    _say(client, token, conversation_id, "早上好呀", "c-seed-2")
    rows = _rows(client, token, conversation_id)

    assert [row["side"] for row in rows] == ["user", "assistant"], rows
    # 玩家这句可能被拼上"系统注：真实时间"的后缀，所以按前缀算
    contents = [str(m.get("content") or "") for m in fake_upstream["seen"][-1]]
    hits = [text for text in contents if text.startswith("早上好呀")]
    assert len(hits) == 1, f"玩家这句出现了 {len(hits)} 次"


def test_echo_window_expired_counts_as_new_message(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔了很久又发同一句：那是真的又说了一遍，不许吞掉。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "在吗", "c-old-3")

    real_utcnow = __import__("app.clock", fromlist=["utcnow"]).utcnow
    monkeypatch.setattr(
        "app.api.conversations.utcnow", lambda: real_utcnow() + timedelta(hours=2)
    )
    _say(client, token, conversation_id, "在吗", "c-old-3b")

    rows = _rows(client, token, conversation_id)
    assert [row["side"] for row in rows] == ["user", "assistant", "user", "assistant"]


def test_echo_can_be_switched_off(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`regenerate_echo_seconds=0` → 退回"每次都新增一条"（应急开关）。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    monkeypatch.setattr(get_settings(), "regenerate_echo_seconds", 0)

    _say(client, token, conversation_id, "嗯", "c-old-5")
    _say(client, token, conversation_id, "嗯", "c-old-5b")

    rows = _rows(client, token, conversation_id)
    assert [row["side"] for row in rows] == ["user", "assistant", "user", "assistant"]


def test_echo_does_not_touch_a_different_line(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """不同的话照常算新消息（这条规则只认"一字不差"）。"""
    _device_id, token = device
    conversation_id = _conversation(client, token)
    _say(client, token, conversation_id, "第一句", "c-old-6")
    _say(client, token, conversation_id, "第二句", "c-old-6b")

    rows = _rows(client, token, conversation_id)
    assert [row["side"] for row in rows] == ["user", "assistant", "user", "assistant"]


def test_echo_skips_group_conversation(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """群聊不适用：群聊那一轮的历史是客户端带上来的，走的是另一套。"""
    _device_id, token = device
    conversation_id = _conversation(client, token, group=True)
    _say(client, token, conversation_id, "大家好", "c-old-7")
    _say(client, token, conversation_id, "大家好", "c-old-7b")

    rows = _rows(client, token, conversation_id)
    users = [row for row in rows if row["side"] == "user"]
    assert len(users) == 2, rows
