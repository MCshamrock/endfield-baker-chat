"""tests/test_legacy.py —— 老客户端兼容层（`/chat`、`/chat/stream`）。

守三件事：
  1. 老契约的请求（无 token、带 history）能正常拿到 `reply`
  2. 流式帧名只有 `delta` / `done`（老客户端只认这两个）
  3. 不存在的 `/group/*` 路径回 404（群聊协议本身已实现，见 test_legacy_group.py）
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import generate, keys
from app.prompt.rules import EMPHASIS_RULE
from app.settings import get_settings
from app.upstream import Reply


@pytest.fixture
def fake_upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"messages": []}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        captured["messages"] = messages
        return Reply(text="(点头)我在。", prompt_tokens=30, completion_tokens=5)

    keys.reset()
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")
    return captured


def _payload(**extra: Any) -> dict[str, Any]:
    body = {
        "character_id": "perlica",
        "message": "在吗",
        "history": [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早，管理员。"},
        ],
        "immersive_mode": False,
    }
    body.update(extra)
    return body


def test_legacy_chat_needs_no_token(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    """老客户端不发 Authorization，也必须能用。"""
    resp = client.post("/chat", json=_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == "(点头)我在。"
    assert body["usage"]["prompt_tokens"] == 30
    assert "mood" in body and "knowledge_source" in body


def test_legacy_chat_uses_history(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    """请求里的 history 要进提示词（老客户端自己就是数据库）。"""
    client.post("/chat", json=_payload())
    messages = fake_upstream["messages"]
    joined = "\n".join(str(item["content"]) for item in messages)
    assert "早，管理员。" in joined


def test_legacy_chat_unknown_character(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    resp = client.post("/chat", json=_payload(character_id="nobody", character=""))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_character"


def _system_text(messages: list[dict[str, object]]) -> str:
    """把这次请求的所有 system 段拼起来（测试里只关心"模型到底看到了什么"）。"""
    return "\n".join(str(item["content"]) for item in messages if item["role"] == "system")


def test_legacy_character_prompt_overrides_the_card(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """前端带 character_prompt（用户改过的整段卡）时：只用它，不再拼库里的卡。

    这是老客户端「自定义人设」唯一的出口 —— pydantic 以前把字段直接丢掉，
    用户改完人设发出去，模型看到的还是官方卡（实测失效）。
    """
    override = "【我自己写的人设】你是测试角色，说话只用一个字。"
    resp = client.post(
        "/chat",
        json=_payload(character_prompt=override, use_new_prompt=True, immersive_mode=False),
    )
    assert resp.status_code == 200, resp.text

    system = _system_text(fake_upstream["messages"])
    assert override in system
    # 库里的佩丽卡卡（v1/v2 都以这句开头）一个字都不该出现
    assert "终末地工业的监督、官方发言人" not in system
    # 用户整段卡不再补服务端的风格规则（否则等于往人家的卡里塞私货）
    assert EMPHASIS_RULE not in system


def test_legacy_system_prompt_fallback_without_card(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """未收录的角色 + system_prompt 兜底：照常出话，**不能 404**。

    老客户端里有一批自建角色只有 system_prompt（历史里也没有官方 slug）。
    """
    fallback = "【兜底人设】你是一个测试用角色，说话简短。"
    resp = client.post(
        "/chat",
        json=_payload(
            character_id="not_in_db",
            character="不存在的人",
            system_prompt=fallback,
        ),
    )
    assert resp.status_code == 200, resp.text
    assert fallback in _system_text(fake_upstream["messages"])


def test_legacy_all_prompts_empty_is_still_404(
    client: TestClient, fake_upstream: dict[str, Any]
) -> None:
    """三者都没有（user 覆盖 / 角色卡 / system 兜底）才是真的 404。"""
    resp = client.post(
        "/chat",
        json=_payload(
            character_id="nobody",
            character="",
            character_prompt="   ",
            system_prompt="  ",
        ),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_character"


def test_legacy_chat_stream_frames(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _stream(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> AsyncIterator[str]:
        yield "我在。"
        yield "怎么了？"

    keys.reset()
    monkeypatch.setattr(generate.upstream, "stream", _stream)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")

    resp = client.post("/chat/stream", json=_payload(immersive_mode=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = [block for block in resp.text.split("\n\n") if block.strip()]
    events = [block.split("\n")[0].removeprefix("event: ") for block in frames]
    assert events == ["delta", "delta", "done"]

    done = json.loads(frames[-1].split("\n")[1].removeprefix("data: "))
    assert done["reply"] == "我在。怎么了？"
    assert "usage" in done


def test_unknown_group_path_is_404(client: TestClient, fake_upstream: dict[str, Any]) -> None:
    """群聊协议已经实现（六个端点），但没写过的路径必须是 404 而不是挂在通配桩上。

    老服务从来就没有 `/group/create` 这种路径，它是当年"501 占位桩"用来验证兜底的。
    现在兜底桩删了，未匹配的路径由 FastAPI 正常回 404 —— 这才是对的行为。
    """
    resp = client.post("/group/create", json={})
    assert resp.status_code == 404


def test_instances_returns_compatible_shape(client: TestClient) -> None:
    """运维脚本读的 `/instances`：形状要对，内容要诚实。

    新后端是单进程，所以每个模块只报一个"实例"，status 用真实探活结果，
    多余字段（mode / keys / note）老脚本会忽略。
    """
    body = client.get("/instances").json()
    assert set(body["modules"]) == {"prompt", "chat"}
    node = body["modules"]["chat"][0]
    assert node["status"] in ("healthy", "down")
    assert node["port"] > 0
    assert body["scaling"] == {"prompt_count": 1, "chat_count": 1}
    assert body["mode"] in ("inline", "gateway")
    assert "note" in body
    assert set(body["keys"]) == {"total", "enabled", "cooldown", "disabled"}


def test_instances_scale_says_no_instead_of_pretending(client: TestClient) -> None:
    body = client.post("/instances/scale", json={"chat": 8}).json()
    assert body["ok"] is False
    assert body["changed"] == []
    assert "单进程" in body["note"]
