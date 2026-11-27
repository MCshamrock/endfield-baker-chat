"""tests/test_group.py —— 群聊：谁说话、能不能停下来、说的算不算数。

群聊最容易出三类错，这三类都会让玩家觉得"假的"：
  1. 永远只有一个人在说（轮转没生效 / 选人写死）
  2. 没人想说话时硬凑一句（END 没被尊重）
  3. 说了却记成别人（speaker 没落库，历史里全变成"角色"）
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import generate, keys
from app.db import SessionLocal
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """主持人返回 END 或指定名字；角色发言返回固定文本。"""
    state: dict[str, Any] = {"count": 0, "judge": '{"next": "END", "reason": "聊完了"}'}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        state["count"] = int(state["count"]) + 1
        system = str(messages[0].get("content", ""))
        if "主持人" in system:
            return Reply(text=str(state["judge"]))
        return Reply(text="（点头）我在。", prompt_tokens=50, completion_tokens=6)

    keys.reset()
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return state


def _create_group(client: TestClient, token: str, **kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "group",
        "members": ["perlica", "chenqianyu"],
        "speak_mode": "round",
        "title": "试验群",
    }
    payload.update(kwargs)
    resp = client.post(
        "/v1/conversations",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_group_requires_two_members(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    resp = client.post(
        "/v1/conversations",
        json={"mode": "group", "members": ["perlica"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_create_group_returns_members(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    group = _create_group(client, token)
    assert group["mode"] == "group"
    assert group["members"] == ["佩丽卡", "陈千语"]
    assert group["speak_mode"] == "round"


def test_round_mode_alternates(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """轮转：第一轮第一个，第二轮第二个。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token)["id"]

    first = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g1", "text": "大家好"},
        headers=headers,
    ).json()
    second = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g2", "text": "今天谁值班"},
        headers=headers,
    ).json()

    assert first["speaker"] == "佩丽卡"
    assert second["speaker"] == "陈千语"
    # 默认沉浸式：括号里的动作描写会被过滤掉，只剩台词
    assert first["reply"] == "我在。"


def test_smart_mode_respects_end(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """主持人说停就真的不发言（不硬凑一句）。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token, speak_mode="smart")["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-end", "text": "嗯"},
        headers=headers,
    ).json()

    assert resp["reply"] is None
    assert resp["silent"] is True
    # 玩家这条仍然存下来了
    listed = client.get(f"/v1/conversations/{conv_id}/messages", headers=headers).json()
    assert len(listed["items"]) == 1


def test_smart_mode_picks_named_member(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    fake_upstream["judge"] = '{"next": "陈千语", "reason": "她更懂"}'
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token, speak_mode="smart")["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-named", "text": "千语你来"},
        headers=headers,
    ).json()
    assert resp["speaker"] == "陈千语"


def test_named_member_speaks_even_if_judge_says_end(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """玩家点名时不让主持人有机会"发挥"——实测过，模型会忽略被点名这件事。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token, speak_mode="smart")["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-mention", "text": "佩丽卡你先说说"},
        headers=headers,
    ).json()
    assert resp["speaker"] == "佩丽卡"
    assert resp["reply"] is not None


def test_assign_speaker_overrides(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token)["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-assign", "text": "点个名", "assign_speaker": "chenqianyu"},
        headers=headers,
    ).json()
    assert resp["speaker"] == "陈千语"


def test_group_reply_records_speaker(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """speaker 必须落库：否则历史里所有 AI 发言都变成"角色"，下一轮就分不清谁说过什么。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token)["id"]
    client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-spk", "text": "在吗"},
        headers=headers,
    )

    with SessionLocal() as db:
        row = db.execute(
            text("SELECT speaker FROM messages WHERE side='assistant' ORDER BY seq DESC LIMIT 1")
        ).scalar()
    assert row == "佩丽卡"


# ---------------------------------------------------------------------------
# B1（2026-11-20 群反馈）：「指定发言」指定的人不接话
#
# 根因：前端 `assign_speaker` 发的是**中文名**（明河 / 骏卫……），后端只按 slug
# 精确比，于是永远比不上 → 打一条 warning，改让主持人随便挑。
# 现场表现就是"指定的 A 不回，回话的是 B"，而且每次都不同，看起来像随机。
#
# 这几条把"名字也必须认"钉死；下面还有一条 `/next` 的（群聊推进按钮走那个端点）。
# ---------------------------------------------------------------------------


def test_assign_speaker_accepts_chinese_name(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """/messages 的 assign_speaker 传中文名也要认（前端就是这么传的）。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token)["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-assign-cn", "text": "点个名", "assign_speaker": "陈千语"},
        headers=headers,
    ).json()
    assert resp["speaker"] == "陈千语"


def test_assign_speaker_tolerates_spaces_and_case(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """老客户端 / 手输场景：前后空格、slug 大小写都不该让"指定"失效。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token)["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "g-assign-pad", "text": "点个名", "assign_speaker": "  ChenQianYu "},
        headers=headers,
    ).json()
    assert resp["speaker"] == "陈千语"


def test_next_accepts_chinese_speaker(
    client: TestClient, device: tuple[str, str], fake_upstream: dict[str, Any]
) -> None:
    """「指定发言」按钮走的是 `/next`，它的 speaker 同样是中文名。"""
    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    conv_id = _create_group(client, token, speak_mode="assign")["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/next",
        json={"client_msg_id": "g-next-cn", "speaker": "佩丽卡"},
        headers=headers,
    ).json()
    assert resp["speaker"] == "佩丽卡"
    assert resp["reply"]


def test_match_member_unit() -> None:
    """纯函数层：slug / 名字 / 大小写 / 空格 / 找不到。"""
    from app.group import match_member

    members = [(1, "chenqianyu", "陈千语", 0), (2, "perlica", "佩丽卡", 1)]
    assert match_member(members, "chenqianyu") == ("chenqianyu", "陈千语")
    assert match_member(members, "陈千语") == ("chenqianyu", "陈千语")
    assert match_member(members, " Perlica ") == ("perlica", "佩丽卡")
    assert match_member(members, "PERLICA") == ("perlica", "佩丽卡")
    # 不在群里：返回 None，由调用方退回主持人/轮转
    assert match_member(members, "明河") is None
    assert match_member(members, "") is None
