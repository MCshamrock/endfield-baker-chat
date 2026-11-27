"""tests/test_weight.py —— 发言权重这条链：默认值、覆盖、恢复默认、选人、会话详情。

守五件事，每一条都对应一个"玩家会当场发现不对"的场景：

  1. 新设备打开角色表：每个角色的 `weight` 必须等于 `active_weight`、
     来源是 `default` —— 不能凭空冒出一个"你改过"。
  2. `PUT` 改完 `GET` 能读到（`user`），`DELETE` 能恢复默认，
     而且**再删一次也得是 200**（幂等，客户端不该报错）。
  3. 权重只影响**群聊选人**，单聊不受影响（单聊只有一个人，没有"选"这回事）。
  4. `GET /v1/conversations/{id}` 的 `member_weights` 要带每个成员的有效权重，
     顺序和 `members` 一一对应。
  5. `choose_speaker` 在权重差距大时明显偏向重的人（固定随机种子 + 宽松阈值，
     不写"必然接近概率"这种会偶发红的断言）。
"""

import random
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import group, keys
from app.db import SessionLocal
from app.models import Character
from app.settings import get_settings
from app.upstream import Reply


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _characters(client: TestClient, token: str | None = None) -> dict[str, dict[str, Any]]:
    """拉角色表，转成 `{slug: item}` 方便按 id 取。token 为 None 时不带身份。"""
    resp = client.get("/v1/characters", headers=_headers(token) if token else {})
    assert resp.status_code == 200, resp.text
    return {item["id"]: item for item in resp.json()["items"]}


def _default_weight(slug: str) -> int:
    """直接查库拿角色自带的 active_weight（测试的"标准答案"）。"""
    with SessionLocal() as db:
        row = db.scalar(select(Character).where(Character.slug == slug))
        assert row is not None, f"测试库里没有角色 {slug}"
        return row.active_weight


def _new_group(client: TestClient, token: str, mode: str = "round") -> dict[str, Any]:
    resp = client.post(
        "/v1/conversations",
        json={
            "mode": "group",
            "members": ["perlica", "chenqianyu"],
            "speak_mode": mode,
        },
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """把上游模型换成一个固定回一句的假函数（测试不连真网）。"""

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        return Reply(text="（点头）我在。", prompt_tokens=20, completion_tokens=5)

    keys.reset()
    monkeypatch.setattr("app.generate.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")


# ---------------------------------------------------------------- 1. 默认值
def test_new_device_sees_default_weights(client: TestClient, device: tuple[str, str]) -> None:
    """新设备：每个角色 weight == active_weight，来源全是 default。"""
    _device_id, token = device

    items = _characters(client, token)
    assert items, "角色表不该是空的"
    for slug, item in items.items():
        assert item["weight"] == item["active_weight"], f"{slug} 默认值不该被改写"
        assert item["weight_source"] == "default", f"{slug} 没改过就该是 default"

    # 不带 token 的公开请求也一样：游客按角色默认值
    anonymous = _characters(client)
    for slug, item in anonymous.items():
        assert item["weight"] == item["active_weight"], slug
        assert item["weight_source"] == "default", slug


# ---------------------------------------------------------------- 2. 覆盖 / 恢复
def test_put_get_delete_restores_default(client: TestClient, device: tuple[str, str]) -> None:
    """PUT 80 → GET 变 80/user → DELETE 回默认/default → 再 DELETE 仍 200。"""
    _device_id, token = device
    default = _default_weight("perlica")

    put = client.put(
        "/v1/characters/perlica/weight",
        json={"active": 80},
        headers=_headers(token),
    )
    assert put.status_code == 200, put.text
    assert put.json() == {"character_id": "perlica", "active": 80, "source": "user"}

    item = _characters(client, token)["perlica"]
    assert item["weight"] == 80
    assert item["weight_source"] == "user"
    # 角色默认值本身不许被玩家改动（那是全局的，只有后台能改）
    assert item["active_weight"] == default

    # 换一台设备（另一个玩家）看到的还应该是默认值 —— 覆盖是玩家级的
    other = client.post("/v1/auth/device", json={"platform": "pytest"})
    assert other.status_code == 201, other.text
    other_item = _characters(client, other.json()["token"])["perlica"]
    assert other_item["weight"] == default
    assert other_item["weight_source"] == "default"

    deleted = client.delete("/v1/characters/perlica/weight", headers=_headers(token))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {
        "character_id": "perlica",
        "active": default,
        "source": "default",
    }

    restored = _characters(client, token)["perlica"]
    assert restored["weight"] == default
    assert restored["weight_source"] == "default"

    # 幂等：本来就没覆盖了，再删一次还是 200，返回同样的默认值
    again = client.delete("/v1/characters/perlica/weight", headers=_headers(token))
    assert again.status_code == 200, again.text
    assert again.json() == deleted.json()


# ---------------------------------------------------------------- 3. 单聊不受影响
def test_weight_does_not_affect_single_chat(
    client: TestClient, device: tuple[str, str], fake_upstream: None
) -> None:
    """权重只作用于群聊选人；单聊里她该说话就说话，不会被权重压住。"""
    _device_id, token = device
    # 故意把 perlica 调到最低、chenqianyu 调到最高
    for slug, active in (("perlica", 1), ("chenqianyu", 100)):
        resp = client.put(
            f"/v1/characters/{slug}/weight", json={"active": active}, headers=_headers(token)
        )
        assert resp.status_code == 200, resp.text

    created = client.post(
        "/v1/conversations",
        json={"character_id": "perlica"},
        headers=_headers(token),
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    assert created.json()["mode"] == "single"

    detail = client.get(
        f"/v1/conversations/{conversation_id}", headers=_headers(token)
    ).json()
    assert detail["members"] == []
    assert detail["member_weights"] == [], "单聊没有群成员权重，别造字段"

    sent = client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"client_msg_id": "w-single-1", "text": "在吗"},
        headers=_headers(token),
    )
    assert sent.status_code == 200, sent.text
    body = sent.json()
    # 单聊默认沉浸式会把括号里的动作描写去掉，所以这里只认正文
    assert body["reply"] == "我在。"
    assert body["speaker"] == "", "单聊没有'这一句是谁说的'，不该冒出别的角色"


# ---------------------------------------------------------------- 4. 群聊详情
def test_group_detail_exposes_effective_weights(
    client: TestClient, device: tuple[str, str]
) -> None:
    """群聊详情：members 仍是字符串名数组，member_weights 与它一一对应。"""
    _device_id, token = device
    perlica_default = _default_weight("perlica")
    chen_default = _default_weight("chenqianyu")

    created = _new_group(client, token)
    conversation_id = created["id"]
    assert created["members"] == ["佩丽卡", "陈千语"], "members 必须保持字符串数组（老形状）"

    # 建群响应就带权重（默认值），刚建完群弹调权重界面不用再请求一次
    created_weights = created["member_weights"]
    assert [item["character_id"] for item in created_weights] == ["perlica", "chenqianyu"]
    assert created_weights[0]["weight"] == perlica_default

    # 改掉其中一个，再看详情
    client.put(
        "/v1/characters/perlica/weight", json={"active": 80}, headers=_headers(token)
    )
    detail = client.get(
        f"/v1/conversations/{conversation_id}", headers=_headers(token)
    ).json()
    assert detail["members"] == ["佩丽卡", "陈千语"]
    weights = detail["member_weights"]
    assert len(weights) == len(detail["members"]), "顺序必须一一对应"
    assert [item["name"] for item in weights] == detail["members"]

    assert weights[0]["character_id"] == "perlica"
    assert weights[0]["weight"] == 80
    assert weights[0]["weight_source"] == "user"

    assert weights[1]["character_id"] == "chenqianyu"
    assert weights[1]["weight"] == chen_default
    assert weights[1]["weight_source"] == "default"


# ---------------------------------------------------------------- 5. 选人分布
MEMBERS = [(1, "perlica", "佩丽卡", 0), (2, "chenqianyu", "陈千语", 1)]


def test_choose_speaker_prefers_heavy_weight() -> None:
    """100 : 1 的权重下，抽 300 次应该绝大多数是重的那个人。

    固定种子 + 宽松阈值（只要求 ≥ 240/300，约 80%，理论值约 99%）：
    这条测的是"权重真的进了 `random.choices`"，不是统计精度，别写成必然概率。
    """
    random.seed(20261118)
    picks = [
        group.choose_speaker(MEMBERS, weights={1: 100, 2: 1}, spoken=set())
        for _ in range(300)
    ]
    # choose_speaker 返回 (slug, 显示名)，所以 [0] 是 slug
    heavy = sum(1 for item in picks if item is not None and item[0] == "perlica")
    assert heavy >= 240, f"重权重只被抽到 {heavy}/300，权重可能没生效"
    assert all(item is not None for item in picks)


# ---------------------------------------------------------------- 附：老群聊也同源
def test_legacy_weighted_uses_player_override(
    client: TestClient, device: tuple[str, str], fake_upstream: None
) -> None:
    """老群聊 weighted 分支：赢了覆盖就用覆盖值，不能还按 50 糊弄。"""
    _device_id, token = device
    for slug, active in (("perlica", 100), ("chenqianyu", 1)):
        resp = client.put(
            f"/v1/characters/{slug}/weight", json={"active": active}, headers=_headers(token)
        )
        assert resp.status_code == 200, resp.text

    created = client.post(
        "/group/session",
        json={
            "members": ["佩丽卡", "陈千语"],
            "speak_mode": "weighted",
            "my_role": "admin",
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    random.seed(42)
    reply = client.post(
        "/group/reply",
        json={
            "session_id": session_id,
            "history": [{"side": "mine", "text": "你们谁先说？", "speaker": ""}],
            "want_suggestions": False,
        },
        headers=_headers(token),
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["replies"], body
    assert body["replies"][0]["speaker"] == "佩丽卡", "覆盖成 100 的人应该被抽中"
