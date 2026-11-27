"""tests/test_time_awareness.py —— 「时间感知」开关（time_enabled）。

守两件事，缺一不可：

  1. **提示词层**：`local_time` 是空串时，整段【现在·系统时钟】一个字都不许出现
     （不是让它别报时，是模型根本不知道）。有值时才出现，并且贴在最后一条 user 上。
  2. **接口层**：`time_enabled=False` 时，即使客户端把 `local_time` 传上来了也要丢掉；
     老客户端**不传**这个字段时仍然按「开」处理（行为与改动前完全一致）。

为什么接口层也要测：提示词函数本来就只认 `local_time`，"丢掉时间"这件事发生在
API 层（`conversations.py` / `legacy.py` / `legacy_group.py`）。只测提示词的话，
把 API 那行改坏了测试也发现不了。
"""

import pytest
from fastapi.testclient import TestClient

from app import generate, keys
from app.prompt.dialog import NO_TIME_NOTE, build_dialog_messages
from app.prompt.group import build_group_messages
from app.settings import get_settings
from app.upstream import Reply

# 提示词里的系统时钟标记（模型能看见时间全靠这一段）
CLOCK_MARK = "【现在·系统时钟】"
# 关掉时间感知时用来"堵住编造"的那段（模型看见它才知道自己不知道）
NO_TIME_MARK = "【没有时间信息】"
# 测试用的固定时刻（带时段词，和 local_time_text() 的格式一致）
STAMP = "2026-11-18 03:40 周三"


def _joined(messages: list[dict[str, object]]) -> str:
    """把整份 messages 拼成一串，便于断言"有没有出现某段文字"。"""
    return "\n".join(str(item.get("content") or "") for item in messages)


# ---------------------------------------------------------------- 提示词层
def test_dialog_prompt_without_time_has_no_clock() -> None:
    messages = build_dialog_messages(
        character_prompt="角色设定",
        history=[{"role": "assistant", "content": "我在。"}],
        message="几点了？",
        local_time="",
        immersive=False,
    )
    assert CLOCK_MARK not in _joined(messages)
    assert "真实时间" not in _joined(messages)


def test_dialog_prompt_with_time_has_clock() -> None:
    messages = build_dialog_messages(
        character_prompt="角色设定",
        history=[{"role": "assistant", "content": "我在。"}],
        message="几点了？",
        local_time=STAMP,
        time_enabled=True,
        immersive=False,
    )
    joined = _joined(messages)
    assert CLOCK_MARK in joined
    assert STAMP in joined
    # 时间必须"贴着"最后一条 user（历史里出现过别的时间时，模型会顺着编）
    assert messages[-1]["role"] == "user"
    assert STAMP in str(messages[-1]["content"])


def test_dialog_prompt_time_disabled_says_it_has_no_time() -> None:
    """关了时间感知又问了时间：不给时刻，但必须明确告诉模型"你没有时间信息"。

    少了这一段，模型会编一个具体时刻（2026-11-18 实测："上午九点三十二分"）。
    """
    messages = build_dialog_messages(
        character_prompt="角色设定",
        history=[{"role": "assistant", "content": "我在。"}],
        message="几点了？",
        local_time="",  # 关掉时前端不再发时间
        time_enabled=False,
        immersive=False,
    )
    joined = _joined(messages)
    assert CLOCK_MARK not in joined
    assert NO_TIME_MARK in joined
    assert NO_TIME_NOTE in joined
    # 不能把"不知道"塞进玩家那句话里冒充玩家发言
    assert NO_TIME_MARK not in str(messages[-1]["content"])


def test_dialog_prompt_time_disabled_without_intent_is_silent() -> None:
    """没问时间就别每轮都念一遍"我不知道几点了"（她会自己主动提，很出戏）。"""
    messages = build_dialog_messages(
        character_prompt="角色设定",
        message="今天吃什么？",
        local_time="",
        time_enabled=False,
        immersive=False,
    )
    joined = _joined(messages)
    assert CLOCK_MARK not in joined
    assert NO_TIME_MARK not in joined


def test_group_prompt_without_time_has_no_clock() -> None:
    messages = build_group_messages(
        speaker="佩丽卡",
        character_prompt="角色设定",
        members=["佩丽卡", "陈千语"],
        player_role="管理员",
        transcript="管理员：几点了？",
        player_message="几点了？",
        local_time="",
    )
    assert CLOCK_MARK not in _joined(messages)


def test_group_prompt_time_disabled_says_it_has_no_time() -> None:
    messages = build_group_messages(
        speaker="佩丽卡",
        character_prompt="角色设定",
        members=["佩丽卡", "陈千语"],
        player_role="管理员",
        transcript="管理员：几点了？",
        player_message="几点了？",
        local_time="",
        time_enabled=False,
    )
    joined = _joined(messages)
    assert CLOCK_MARK not in joined
    assert NO_TIME_MARK in joined


def test_group_prompt_with_time_has_clock() -> None:
    messages = build_group_messages(
        speaker="佩丽卡",
        character_prompt="角色设定",
        members=["佩丽卡", "陈千语"],
        player_role="管理员",
        transcript="管理员：几点了？",
        player_message="几点了？",
        local_time=STAMP,
    )
    assert CLOCK_MARK in _joined(messages)
    assert STAMP in _joined(messages)


# ---------------------------------------------------------------- 接口层
@pytest.fixture
def captured_upstream(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, object]]]:
    """把上游换成假函数，并把每次收到的 messages 原样记下来（不联网、不花钱）。"""
    calls: list[list[dict[str, object]]] = []

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        calls.append(messages)
        return Reply(text="（点头）现在大概是凌晨。", prompt_tokens=10, completion_tokens=8)

    keys.reset()  # key 池有 10 秒缓存，测试之间必须清
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return calls


def _new_conversation(client: TestClient, token: str) -> str:
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "perlica"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _send(
    client: TestClient,
    token: str,
    conv_id: str,
    payload: dict[str, object],
) -> None:
    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "c-time", "text": "几点了", "immersive": False, **payload},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


def test_time_enabled_false_drops_client_time(
    client: TestClient, device: tuple[str, str], captured_upstream: list[list[dict[str, object]]]
) -> None:
    """关了时间感知：客户端把 local_time 传上来也必须被丢掉，提示词里没有时钟。"""
    _device_id, token = device
    conv_id = _new_conversation(client, token)

    _send(
        client,
        token,
        conv_id,
        {"local_time": STAMP, "time_enabled": False},
    )

    assert captured_upstream, "上游没有被调用"
    joined = _joined(captured_upstream[-1])
    assert CLOCK_MARK not in joined
    assert STAMP not in joined
    # 关键：不是"没给时间"，而是"明确告诉它没有时间信息"（否则模型会编）
    assert NO_TIME_MARK in joined


def test_time_enabled_true_keeps_client_time(
    client: TestClient, device: tuple[str, str], captured_upstream: list[list[dict[str, object]]]
) -> None:
    _device_id, token = device
    conv_id = _new_conversation(client, token)

    _send(client, token, conv_id, {"local_time": STAMP, "time_enabled": True})

    assert captured_upstream
    assert CLOCK_MARK in _joined(captured_upstream[-1])


def test_legacy_default_keeps_server_time(
    client: TestClient, device: tuple[str, str], captured_upstream: list[list[dict[str, object]]]
) -> None:
    """老客户端**不传** time_enabled：服务端自己补时间，行为与改动前一致。"""
    _device_id, token = device
    resp = client.post(
        "/chat",
        json={"character_id": "perlica", "message": "几点了"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert captured_upstream
    assert CLOCK_MARK in _joined(captured_upstream[-1])
