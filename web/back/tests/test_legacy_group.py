"""tests/test_legacy_group.py —— 老客户端群聊协议（`/group/*`）能不能原样跑。

守的是"老客户端一行不改"这件事：字段名、done 语义、错误码、
round 一次给全员、smart 由主持人决定停不停。这些只要有一处对不上，
老客户端就会表现成"点了没反应""群里有人不说话""自己停了"。

协议来源：老服务 `group_chat_service.py`（5810），见 docs/06-老群聊协议.md。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import generate, keys
from app.api import legacy_group
from app.settings import get_settings
from app.upstream import Reply, UpstreamError

MEMBERS = ["佩丽卡", "陈千语"]


@pytest.fixture(autouse=True)
def clean_sessions() -> None:
    """每个用例都从空会话表开始，免得 TTL 之外的残留互相干扰。"""
    legacy_group._SESSIONS.clear()


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """三种调用形态：主持人判断 / 输入推荐 / 角色发言。"""
    state: dict[str, Any] = {"judge": '{"next": "陈千语"}', "count": 0, "messages": []}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        state["count"] += 1
        state["messages"].append(messages)
        system = str(messages[0].get("content", ""))
        if "主持人" in system:
            return Reply(text=str(state["judge"]))
        if "接下来可以说什么" in system:
            return Reply(text='["嗯。", "然后呢？", "我知道了。"]')
        return Reply(text="（点头）我在。", prompt_tokens=40, completion_tokens=6)

    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    keys.reset()
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _create(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"members": MEMBERS, "speak_mode": "round", "my_role": "admin"}
    payload.update(kwargs)
    resp = client.post("/group/session", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _history(player_line: str = "你们怎么都不说话") -> list[dict[str, str]]:
    return [
        {"side": "other", "text": "今天天气不错。", "speaker": "佩丽卡"},
        {"side": "mine", "text": player_line, "speaker": ""},
    ]


# ---------------------------------------------------------------- 建群
def test_create_session_returns_legacy_shape(client: TestClient) -> None:
    body = _create(client)
    assert body["ok"] is True
    assert body["session_id"]
    assert body["members"] == MEMBERS
    assert body["speak_mode"] == "round"
    assert body["my_role"] == "admin"
    assert body["ttl"] == legacy_group.SESSION_TTL
    assert body["assign_target"] == MEMBERS[0]


def test_create_rejects_too_few_members(client: TestClient) -> None:
    resp = client.post("/group/session", json={"members": ["佩丽卡"]})
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_members"


def test_create_rejects_bad_role(client: TestClient) -> None:
    resp = client.post("/group/session", json={"members": MEMBERS, "my_role": "不存在的人"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_my_role"


# ---------------------------------------------------------------- 发言
def test_round_returns_every_member(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    """round 是老协议里唯一"一次请求多次调用上游"的模式：必须全员各一条。"""
    session = _create(client, speak_mode="round")
    resp = client.post(
        "/group/reply", json={"session_id": session["session_id"], "history": _history()}
    )
    body = resp.json()
    assert resp.status_code == 200
    assert [item["speaker"] for item in body["replies"]] == MEMBERS
    assert body["done"] is True
    assert body["stop_code"] == ""
    for item in body["replies"]:
        assert set(item) >= {"speaker", "text", "need_search", "knowledge_source", "usage"}


def test_assign_returns_only_target(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    session = _create(client, speak_mode="assign", assign_target="陈千语")
    resp = client.post(
        "/group/reply", json={"session_id": session["session_id"], "history": _history()}
    )
    body = resp.json()
    assert len(body["replies"]) == 1
    assert body["replies"][0]["speaker"] == "陈千语"
    assert body["done"] is True


def test_smart_returns_one_and_continues(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """smart：一次一条，done=False 表示前端该接着请求。"""
    session = _create(client, speak_mode="smart")
    resp = client.post(
        "/group/reply", json={"session_id": session["session_id"], "history": _history()}
    )
    body = resp.json()
    assert len(body["replies"]) == 1
    assert body["replies"][0]["speaker"] == "陈千语"  # 主持人指定的人
    assert body["done"] is False


def test_smart_end_continues_instead_of_stopping(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """主持人判 END 时**不收摊**，改用续接指令换下一位接着说（老服务的行为）。

    老服务的口径：只有玩家自己点「暂停对话」才停。判 END 只是"这个话题聊完了"，
    它会让上一位的下一位带着 NUDGE_CONTINUE 把话题自然续下去。
    """
    fake_upstream["judge"] = '{"next": "END", "reason": "聊完了"}'
    session = _create(client, speak_mode="smart")
    body = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": _history(),
            "want_suggestions": False,
        },
    ).json()

    assert len(body["replies"]) == 1
    assert body["replies"][0]["speaker"] == "佩丽卡"  # 上一位为空 → 环状取第一位
    assert body["done"] is False
    # 续接指令确实进了这一次的提示词
    joined = "\n".join(
        str(item.get("content", "")) for item in fake_upstream["messages"][-1]
    )
    assert legacy_group.NUDGE_CONTINUE in joined


def test_round_member_failure_does_not_kill_the_round(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """round 里一个成员失败：只留一条空占位，其余照说，整轮仍 200 + done。"""
    calls = {"n": 0}

    async def _fake_upstream_call(
        db: Any, messages: Any, *, immersive: bool, think: bool
    ) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise UpstreamError("模拟第 1 个成员失败")
        return generate.Generation(text="我在。")

    monkeypatch.setattr("app.api.legacy_group.upstream_call", _fake_upstream_call)

    session = _create(client, speak_mode="round")
    resp = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": _history(),
            "want_suggestions": False,
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert [item["speaker"] for item in body["replies"]] == MEMBERS
    assert body["replies"][0]["text"] == ""
    assert "error" in body["replies"][0]
    assert body["replies"][1]["text"] == "我在。"
    assert body["done"] is True


def test_assign_player_role_is_not_replaced(client: TestClient) -> None:
    """assign 点到玩家自己扮演的角色：不能换别人顶替，要回 player_role 停止原因。"""
    session = _create(
        client, speak_mode="assign", my_role="佩丽卡", assign_target="佩丽卡"
    )
    resp = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": _history(),
            "want_suggestions": False,
        },
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["replies"] == []
    assert body["done"] is True
    assert body["stop_code"] == "player_role"
    assert "玩家本人扮演" in body["stop_reason"]


def test_total_turns_cap_stops_the_group(client: TestClient) -> None:
    """累计轮数到 600 就收摊：new_turn 清的是 turns，清不掉 total_turns。"""
    session = _create(client, speak_mode="round")
    legacy_group._SESSIONS[session["session_id"]].total_turns = legacy_group.MAX_TOTAL_TURNS
    body = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": _history(),
            "want_suggestions": False,
        },
    ).json()
    assert body["replies"] == []
    assert body["done"] is True
    assert body["stop_code"] == "max_total_turns"
    assert "上限" in body["stop_reason"]


def test_group_image_history_becomes_placeholder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯图片的历史项不能被丢掉：转成老服务那句占位文字再送给模型。"""
    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"role": "user", "content": "占位"}]

    async def _fake_upstream_call(
        db: Any, messages: Any, *, immersive: bool, think: bool
    ) -> Any:
        return generate.Generation(text="嗯。")

    monkeypatch.setattr("app.api.legacy_group.build_group_messages", _fake_build)
    monkeypatch.setattr("app.api.legacy_group.upstream_call", _fake_upstream_call)

    session = _create(client, speak_mode="assign", assign_target="佩丽卡")
    history = [
        {"side": "mine", "text": "", "speaker": "", "image": "data:image/png;base64,AAAA"},
        {"side": "other", "text": "", "speaker": "陈千语", "image": "data:image/png;base64,BBBB"},
        {"side": "mine", "text": "你们怎么都不说话", "speaker": ""},
    ]
    resp = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": history,
            "want_suggestions": False,
        },
    )
    assert resp.status_code == 200, resp.text
    transcript = str(captured["transcript"])
    assert "[玩家发来一张图片]" in transcript
    assert "[图片]" in transcript


def test_player_role_member_is_not_spoken_for(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """玩家自己扮演的成员不该被 AI 抢去说话。"""
    session = _create(client, speak_mode="round", my_role="佩丽卡")
    resp = client.post(
        "/group/reply", json={"session_id": session["session_id"], "history": _history()}
    )
    speakers = [item["speaker"] for item in resp.json()["replies"]]
    assert speakers == ["陈千语"]


def test_reply_on_unknown_session_is_404_with_flag(client: TestClient) -> None:
    resp = client.post("/group/reply", json={"session_id": "nope", "history": []})
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "session_expired"
    assert body["session_expired"] is True


def test_reply_updates_config_in_place(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    """请求里带 assign_target 就当场换人（老服务的"换人失败"就是这么修的）。"""
    session = _create(client, speak_mode="assign")
    resp = client.post(
        "/group/reply",
        json={
            "session_id": session["session_id"],
            "history": _history(),
            "assign_target": "陈千语",
        },
    )
    assert resp.json()["replies"][0]["speaker"] == "陈千语"


def test_max_turns_forces_stop(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    session = _create(client, speak_mode="smart")
    sid = session["session_id"]
    for _ in range(legacy_group.MAX_TURNS):
        client.post("/group/reply", json={"session_id": sid, "history": _history()})
    body = client.post("/group/reply", json={"session_id": sid, "history": _history()}).json()
    assert body["done"] is True
    assert body["stop_code"] == "max_turns"


# ---------------------------------------------------------------- 推荐 / 话题 / 关闭
def test_suggestions_return_three(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    session = _create(client)
    resp = client.post(
        "/group/suggestions",
        json={"session_id": session["session_id"], "history": _history()},
    )
    body = resp.json()
    assert body["ok"] is True
    assert len(body["suggestions"]) == 3


def test_suggestions_empty_for_observer(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """旁观模式不生成推荐（老需求：旁观不参与对话）。"""
    session = _create(client, my_role="observer")
    body = client.post(
        "/group/suggestions",
        json={"session_id": session["session_id"], "history": _history()},
    ).json()
    assert body["suggestions"] == []


def test_topic_and_close(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    session = _create(client)
    sid = session["session_id"]
    body = client.post("/group/topic", json={"session_id": sid, "topic": "去食堂"}).json()
    assert body["topic"] == "去食堂"

    assert client.post("/group/session/close", json={"session_id": sid}).json()["closed"] is True
    # 关掉之后再发就该 404
    assert (
        client.post("/group/reply", json={"session_id": sid, "history": []}).status_code == 404
    )


def test_list_sessions_has_counts(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    _create(client)
    body = client.get("/group/sessions").json()
    assert body["count"] == 1
    assert body["sessions"][0]["members"] == MEMBERS
