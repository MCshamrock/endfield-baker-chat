"""tests/test_suggestions.py —— 输入推荐（三个入口共用一套逻辑）。

守四件事：
  1. 老客户端 `/chat/suggestions` 能拿到 3 条（这是替代旧后端缺的那一块）
  2. 上游挂掉时**返回空列表而不是报错** —— 推荐不该让聊天界面弹错
  3. 新客户端 `/v1/.../suggestions` 要按归属鉴权（别人的会话看不到）
  4. 解析器对脏输出（没 JSON、超长、空串）不炸
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.prompt.suggest import parse_suggestions
from app.settings import get_settings
from app.upstream import Reply, UpstreamError

THREE = '["嗯，然后呢？", "所以你到底想说什么", "算了，先不说这个"]'


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"fail": False, "seen": []}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        state["seen"].append(messages)
        if state["fail"]:
            raise UpstreamError("模拟上游挂了", status=500)
        return Reply(text=THREE)

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 老客户端
def test_legacy_suggestions_returns_three(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    resp = client.post(
        "/chat/suggestions",
        json={
            "character_id": "perlica",
            "history": [
                {"role": "user", "content": "今天好累"},
                {"role": "assistant", "content": "累就歇会儿。"},
            ],
            "message": "可是还有一堆事",
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["suggestions"]
    assert len(items) == 3
    assert all(isinstance(item, str) and item for item in items)


def test_legacy_suggestions_swallow_upstream_failure(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    fake_upstream["fail"] = True
    resp = client.post("/chat/suggestions", json={"character_id": "perlica", "history": []})
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


# ---------------------------------------------------------------- 新客户端
def test_v1_suggestions_for_own_conversation(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]
    client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "s-1", "text": "今天好累"},
        headers=_headers(token),
    )

    resp = client.post(
        f"/v1/conversations/{public_id}/suggestions", headers=_headers(token)
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["suggestions"]) == 3

    # 提示词里应该带上了最近那句
    material = str(fake_upstream["seen"][-1][1]["content"])
    assert "今天好累" in material


def test_v1_suggestions_reject_other_people_conversation(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]

    other = client.post("/v1/auth/device", json={"platform": "pytest"}).json()["token"]
    resp = client.post(f"/v1/conversations/{public_id}/suggestions", headers=_headers(other))
    assert resp.status_code == 404


def test_v1_suggestions_empty_when_upstream_down(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(token)
    ).json()["id"]
    fake_upstream["fail"] = True
    resp = client.post(
        f"/v1/conversations/{public_id}/suggestions", headers=_headers(token)
    )
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []


# ---------------------------------------------------------------- 解析器
def test_parser_tolerates_dirty_output() -> None:
    assert parse_suggestions("没有任何 JSON") == []
    assert parse_suggestions('["只有一条"]') == ["只有一条"]
    assert parse_suggestions('["一","二","三","四"]') == ["一", "二", "三"]
    long_item = "很" * 100
    assert len(parse_suggestions(f'["{long_item}"]')[0]) == 25
