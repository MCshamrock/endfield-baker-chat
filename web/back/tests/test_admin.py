"""tests/test_admin.py —— 后台数据接口。

守四件事：
  1. **没有管理员权限就是 403**（普通账号、访客都不行）
  2. 概览给的是全站真实数字，不是占位
  3. 密钥接口**永远不回明文**（连密文都不回）
  4. 改配置立刻生效（缓存被清），改角色/用户只动一列

后台只做数据接口，所以这里只验 JSON 形状与权限，不验页面。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import keys
from app.db import SessionLocal
from app.models import ApiKey, Character, User
from app.repos import content as content_repo
from app.settings import get_settings
from app.upstream import Reply


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _promote(username: str) -> None:
    with SessionLocal() as db:
        row = db.scalar(select(User).where(User.login_id == username))
        assert row is not None
        row.role = "admin"
        db.commit()


@pytest.fixture
def admin_token(client: TestClient) -> str:
    """注册一个账号、提成管理员，返回它的设备 token。"""
    token = client.post("/v1/auth/device", json={"platform": "pytest"}).json()["token"]
    resp = client.post(
        "/v1/auth/register",
        json={"username": "后台管理员", "password": "admin-pass-123"},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    _promote("后台管理员")
    return token


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        return Reply(text="（点头）在。", reasoning="她在想这个人怎么又来了。")

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return {}


# ---------------------------------------------------------------- 权限
def test_guest_is_rejected(client: TestClient) -> None:
    token = client.post("/v1/auth/device", json={"platform": "pytest"}).json()["token"]
    resp = client.get("/v1/admin/overview", headers=_headers(token))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_normal_user_is_rejected(client: TestClient) -> None:
    token = client.post("/v1/auth/device", json={"platform": "pytest"}).json()["token"]
    client.post(
        "/v1/auth/register",
        json={"username": "普通用户", "password": "user-pass-123"},
        headers=_headers(token),
    )
    resp = client.get("/v1/admin/overview", headers=_headers(token))
    assert resp.status_code == 403


# ---------------------------------------------------------------- 概览 / 用量
def test_overview_shape(client: TestClient, admin_token: str) -> None:
    body = client.get("/v1/admin/overview", headers=_headers(admin_token)).json()
    assert set(body) >= {"counts", "keys", "today", "days", "now"}
    assert body["counts"]["characters"] >= 1
    assert set(body["keys"]) == {"total", "enabled", "cooldown", "disabled"}
    assert set(body["today"]) >= {"requests", "prompt_tokens", "completion_tokens", "avg_ms"}


def test_usage_days_bounded(client: TestClient, admin_token: str) -> None:
    body = client.get("/v1/admin/usage?days=999", headers=_headers(admin_token)).json()
    assert body["days"] == 90


# ---------------------------------------------------------------- 用户
def test_user_list_and_patch(client: TestClient, admin_token: str) -> None:
    body = client.get("/v1/admin/users", headers=_headers(admin_token)).json()
    assert body["total"] >= 1
    target = next(item for item in body["items"] if item["username"] == "后台管理员")
    assert target["role"] == "admin"

    resp = client.patch(
        f"/v1/admin/users/{target['id']}",
        json={"display_name": "三叶草"},
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200 and resp.json()["display_name"] == "三叶草"


def test_cannot_demote_self(client: TestClient, admin_token: str) -> None:
    body = client.get("/v1/admin/users", headers=_headers(admin_token)).json()
    me = next(item for item in body["items"] if item["username"] == "后台管理员")
    resp = client.patch(
        f"/v1/admin/users/{me['id']}", json={"role": "user"}, headers=_headers(admin_token)
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "cannot_demote_self"


# ---------------------------------------------------------------- 角色
def test_character_toggle_and_weight(client: TestClient, admin_token: str) -> None:
    body = client.get("/v1/admin/characters", headers=_headers(admin_token)).json()
    first = body["items"][0]
    resp = client.patch(
        f"/v1/admin/characters/{first['id']}",
        json={"active_weight": 12, "enabled": False},
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["active_weight"] == 12 and resp.json()["enabled"] is False

    # 改回去，别影响别的用例
    with SessionLocal() as db:
        row = db.scalar(select(Character).where(Character.slug == first["id"]))
        assert row is not None
        row.enabled = 1
        db.commit()


# ---------------------------------------------------------------- 配置
def test_config_put_takes_effect_immediately(client: TestClient, admin_token: str) -> None:
    before = client.get("/v1/admin/config", headers=_headers(admin_token)).json()["items"]
    old = next((item["value"] for item in before if item["key"] == "safety_protocol"), "")

    resp = client.put(
        "/v1/admin/config/safety_protocol",
        json={"value": "新的安全协议正文"},
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200

    with SessionLocal() as db:
        # 缓存被清过，所以这次读到的就是新值
        assert content_repo.get_safety_protocol(db) == "新的安全协议正文"

    client.put(
        "/v1/admin/config/safety_protocol",
        json={"value": old},
        headers=_headers(admin_token),
    )


# ---------------------------------------------------------------- 密钥
def test_keys_never_expose_secrets(client: TestClient, admin_token: str) -> None:
    with SessionLocal() as db:
        if not db.scalar(select(ApiKey)):
            db.add(
                ApiKey(
                    label="测试用",
                    key_enc=b"ciphertext",
                    base_url="https://example.com",
                    model="agnes-2.5-flash",
                    rpm=10,
                    max_concurrent=1,
                    enabled=1,
                    fail_count=0,
                    created_at=__import__("app.clock", fromlist=["utcnow"]).utcnow(),
                )
            )
            db.commit()

    body = client.get("/v1/admin/keys", headers=_headers(admin_token)).json()
    assert body["items"]
    item = body["items"][0]
    assert set(item) == {
        "id",
        "label",
        "enabled",
        "cooling",
        "cooldown_until",
        "fail_count",
        "rpm",
        "base_url",
        "model",
        "last_used_at",
    }
    assert "key" not in str(body).lower().replace("keys", "")

    resp = client.patch(
        f"/v1/admin/keys/{item['id']}", json={"enabled": False}, headers=_headers(admin_token)
    )
    assert resp.status_code == 200 and resp.json()["enabled"] is False


# ---------------------------------------------------------------- 思考链
def test_reasoning_endpoint_only_for_admin(
    client: TestClient, admin_token: str, fake_upstream: dict[str, Any]
) -> None:
    public_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=_headers(admin_token)
    ).json()["id"]
    client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": "r-1", "text": "在吗", "think": True},
        headers=_headers(admin_token),
    )

    body = client.get(
        f"/v1/admin/conversations/{public_id}/reasoning", headers=_headers(admin_token)
    ).json()
    assert body["items"]
    assert body["items"][0]["reasoning"].startswith("她在想")

    guest = client.post("/v1/auth/device", json={"platform": "pytest"}).json()["token"]
    assert (
        client.get(
            f"/v1/admin/conversations/{public_id}/reasoning", headers=_headers(guest)
        ).status_code
        == 403
    )
