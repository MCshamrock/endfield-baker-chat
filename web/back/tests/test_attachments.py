"""tests/test_attachments.py —— 附件：上传、去重、鉴权、进 prompt。

三个必须守住的东西：
  1. 同一张图传两次只存一份（按 sha256 去重）—— 否则库里会堆满重复图片；
  2. 别人的图取不到（404，不是 403）—— 附件是全局去重的，归属必须记在设备上；
  3. 带图的一轮进 prompt 时是**多模态数组**，且**历史里的图不重复发送**。
"""

import struct
import zlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import generate, keys
from app.settings import get_settings
from app.upstream import Reply


def png_bytes(width: int = 32, height: int = 32, rgb: tuple[int, int, int] = (220, 40, 40)) -> bytes:
    """手搓一张纯色 PNG（不引 Pillow，测试环境不需要图像库）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def upload(client: TestClient, token: str, data: bytes, mime: str = "image/png") -> Any:
    return client.post(
        "/v1/attachments",
        files={"file": ("a.png", data, mime)},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_upload_and_fetch(
    client: TestClient, device: tuple[str, str]
) -> None:
    _device_id, token = device
    data = png_bytes()
    resp = upload(client, token, data)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["mime"] == "image/png"
    assert body["bytes"] == len(data)
    assert body["dedup"] is False

    got = client.get(
        body["url"], headers={"Authorization": f"Bearer {token}"}
    )
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == data


def test_dedupe_same_content(client: TestClient, device: tuple[str, str]) -> None:
    """同一张图传两次：第二次回同一个 id，且标记 dedup=true。"""
    _device_id, token = device
    data = png_bytes(16, 16)

    first = upload(client, token, data).json()
    second = upload(client, token, data).json()

    assert first["id"] == second["id"]
    assert second["dedup"] is True


def test_rejects_non_image(client: TestClient, device: tuple[str, str]) -> None:
    _device_id, token = device
    resp = upload(client, token, b"hello", mime="text/plain")
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_type"


def test_rejects_empty(client: TestClient, device: tuple[str, str]) -> None:
    _device_id, token = device
    resp = upload(client, token, b"")
    assert resp.status_code == 400


def test_rejects_oversize(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    _device_id, token = device
    resp = upload(client, token, png_bytes(64, 64))
    assert resp.status_code == 413


def test_other_device_cannot_fetch(client: TestClient, device: tuple[str, str]) -> None:
    """别人的图回 404 —— 不泄露"这张图存在"。"""
    _device_id, token = device
    body = upload(client, token, png_bytes(8, 8)).json()

    other = client.post("/v1/auth/device", json={"platform": "pytest-2"}).json()
    resp = client.get(body["url"], headers={"Authorization": f"Bearer {other['token']}"})
    assert resp.status_code == 404


def test_attachment_goes_into_prompt_as_multimodal(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """带图的一轮：发给上游的 content 是多模态数组，里面有一份 image_url。"""
    captured: dict[str, Any] = {}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        captured["messages"] = messages
        return Reply(text="看到了。", prompt_tokens=10, completion_tokens=5)

    keys.reset()
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    attachment = upload(client, token, png_bytes(24, 24)).json()

    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    resp = client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={
            "client_msg_id": "img-1",
            "text": "这是什么颜色",
            "attachment_id": attachment["id"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    messages = captured["messages"]
    last = messages[-1]
    assert last["role"] == "user"
    parts = last["content"]
    assert isinstance(parts, list)
    kinds = [part["type"] for part in parts]
    assert kinds == ["text", "image_url"]
    assert str(parts[1]["image_url"]["url"]).startswith("data:image/png;base64,")


def test_history_images_are_not_resent(
    client: TestClient, device: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """历史里的图不重复发送（每轮重发一次 = 白烧几万 token）。"""
    captured: list[list[dict[str, object]]] = []

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        captured.append(messages)
        return Reply(text="嗯。", prompt_tokens=10, completion_tokens=2)

    keys.reset()
    monkeypatch.setattr(generate.upstream, "complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "gen_mode", "inline")

    _device_id, token = device
    headers = {"Authorization": f"Bearer {token}"}
    attachment = upload(client, token, png_bytes(12, 12)).json()
    conv_id = client.post(
        "/v1/conversations", json={"character_id": "perlica"}, headers=headers
    ).json()["id"]

    # 第一轮带图
    client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "h1", "text": "看图", "attachment_id": attachment["id"]},
        headers=headers,
    )
    # 第二轮纯文字
    client.post(
        f"/v1/conversations/{conv_id}/messages",
        json={"client_msg_id": "h2", "text": "继续"},
        headers=headers,
    )

    second_round = captured[-1]
    for message in second_round:
        if message["role"] == "user":
            assert isinstance(message["content"], str), "历史图片不该再以多模态形式重发"
