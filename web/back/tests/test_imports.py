"""tests/test_imports.py —— 老客户端 / 老网页的数据包导入。

这一组测试的重点不是"能不能导"，而是**导错了会不会悄悄丢数据**：

  * 老包的字段名五花八门（subs / items / role / content / characterCards），得都认
  * 群聊的成员在卡片级，丢了就退化成单聊壳
  * 图片在 ZIP 里是独立文件、在 JSON 里是 dataURL，两条路都得落成附件
  * 同一个包导两次不能把会话建两遍（幂等）
  * 撤销只能撤"这次导入建的"，用户自己聊的不能动
  * 认不出的角色照样建会话（丢历史比丢人设严重）
"""

import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

# 1×1 的合法 PNG（真字节，不是占位符）—— 附件层会算 sha256 并落盘
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wD/9wF1AAAAAElFTkSuQmCC"
)


# ---------------------------------------------------------------- 造包工具
def _zip_bytes(files: dict[str, bytes]) -> bytes:
    """把一堆 {路径: 字节} 压成 ZIP。测试里用它模拟客户端导出的压缩包。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _project_json(**extra: object) -> dict[str, object]:
    """project.json 的最小合法形状（版本必须是 1）。"""
    base: dict[str, object] = {"version": 1, "myGender": "male", "stripVariantIndex": 0}
    base.update(extra)
    return base


def _json_bundle(cards: list[dict[str, object]], **extra: object) -> bytes:
    """单文件 JSON 包（新版形状：{version, cards}）。"""
    payload = _project_json(cards=cards, **extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _post_package(
    client: TestClient,
    token: str,
    data: bytes,
    *,
    filename: str = "BAKER-test.zip",
    mode: str = "merge",
) -> Response:
    """上传一个包。包的类型靠内容判断，content-type 随便填都行。"""
    return client.post(
        "/v1/imports",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (filename, data, "application/octet-stream")},
        data={"mode": mode},
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- JSON 包
def test_import_json_creates_conversation_and_messages(
    client: TestClient, device: tuple[str, str]
) -> None:
    """最基础的一条：一个卡片、一段对话、三条消息，导进来要能原样读出来。"""
    _, token = device
    bundle = _json_bundle(
        [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [
                            {"id": 1, "side": "mine", "text": "在吗"},
                            {"id": 2, "side": "other", "text": "在。说吧"},
                            {"id": 3, "side": "mine", "text": "没事，就是想说句话"},
                        ],
                    }
                ]
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["cards"] == 1
    assert body["conversations"] == 1
    assert body["messages"] == 3
    assert body["source"] == "json"

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert len(conversations) == 1
    assert conversations[0]["title"] == "伊冯"
    assert conversations[0]["character_id"] == "yifeng"

    messages = client.get(
        f"/v1/conversations/{conversations[0]['id']}/messages", headers=_auth(token)
    ).json()["items"]
    # mine → user，other → assistant
    assert [item["side"] for item in messages] == ["user", "assistant", "user"]
    assert [item["text"] for item in messages] == ["在吗", "在。说吧", "没事，就是想说句话"]
    # 单聊的角色消息，说话人缺省就是角色名
    assert messages[1]["speaker"] == "伊冯"


def test_import_group_card_keeps_members_and_mode(
    client: TestClient, device: tuple[str, str]
) -> None:
    """群聊的成员/发言模式是卡片级字段，丢了就退化成单聊壳。"""
    _, token = device
    bundle = _json_bundle(
        [
            {
                "members": ["伊冯", "佩丽卡"],
                "myRole": "observer",
                "speakMode": "round",
                "conversations": [
                    {
                        "name": "群聊",
                        "messages": [
                            {"id": 1, "side": "other", "text": "先说好，今天不加班", "speakerName": "伊冯"},
                            {"id": 2, "side": "other", "text": "你说了不算", "speakerName": "佩丽卡"},
                        ],
                    }
                ],
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert len(conversations) == 1
    group = conversations[0]
    assert group["mode"] == "group"
    assert group["speak_mode"] == "round"
    assert set(group["members"]) == {"伊冯", "佩丽卡"}

    messages = client.get(
        f"/v1/conversations/{group['id']}/messages", headers=_auth(token)
    ).json()["items"]
    assert [item["speaker"] for item in messages] == ["伊冯", "佩丽卡"]


def test_import_legacy_json_field_aliases(client: TestClient, device: tuple[str, str]) -> None:
    """老包的三套字段名：characterCards / subs / items / role+content。"""
    _, token = device
    legacy = {
        "version": "3",
        "characterCards": [
            {
                "subs": [
                    {
                        "title": "伊冯",
                        "items": [
                            {"role": "user", "content": "你好"},
                            {"role": "assistant", "content": "嗯"},
                        ],
                    }
                ]
            }
        ],
    }
    resp = _post_package(
        client, token, json.dumps(legacy, ensure_ascii=False).encode("utf-8"), filename="old.json"
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["messages"] == 2

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert conversations[0]["title"] == "伊冯"


def test_import_accepts_bare_card_array(client: TestClient, device: tuple[str, str]) -> None:
    """最老的格式：文件内容直接就是一个卡片数组。"""
    _, token = device
    bare = [
        {"conversations": [{"name": "佩丽卡", "messages": [{"side": "mine", "text": "到"}]}]}
    ]
    resp = _post_package(
        client, token, json.dumps(bare, ensure_ascii=False).encode("utf-8"), filename="x.json"
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["conversations"] == 1


# ---------------------------------------------------------------- ZIP 包
def test_import_zip_with_separate_image_file(
    client: TestClient, device: tuple[str, str]
) -> None:
    """ZIP 包里图片是独立文件，JSON 里只有包内路径 —— 要能落成附件并取回来。"""
    _, token = device
    conversation = {
        "name": "伊冯",
        "messages": [
            {"id": 1, "side": "mine", "text": "", "image": "cards/00/images/00-1.png"},
            {"id": 2, "side": "other", "text": "拍糊了"},
        ],
    }
    bundle = _zip_bytes(
        {
            "project.json": json.dumps(_project_json(), ensure_ascii=False).encode("utf-8"),
            "cards/00/conversations/00-伊冯.json": json.dumps(
                conversation, ensure_ascii=False
            ).encode("utf-8"),
            "cards/00/images/00-1.png": PNG_1PX,
        }
    )

    resp = _post_package(client, token, bundle)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "zip"
    assert body["images"] == 1
    assert body["messages"] == 2

    # 附件真的落盘了，而且字节一致
    attachments = client.get("/v1/attachments", headers=_auth(token)).json()["items"]
    assert len(attachments) == 1
    image = client.get(f"/v1/attachments/{attachments[0]['id']}", headers=_auth(token))
    assert image.status_code == 200
    assert image.content == PNG_1PX

    # 消息本身也要指到那张图，否则重新进会话时图片消息就是一条空气泡
    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    messages = client.get(
        f"/v1/conversations/{conversations[0]['id']}/messages", headers=_auth(token)
    ).json()["items"]
    assert messages[0]["attachment_id"] == attachments[0]["id"]
    assert messages[0]["attachment_url"] == f"/v1/attachments/{attachments[0]['id']}"
    assert messages[1]["attachment_id"] is None


def test_import_json_inline_data_url_image(client: TestClient, device: tuple[str, str]) -> None:
    """JSON 包的图是内联 dataURL，同样要落成附件。"""
    _, token = device
    data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode("ascii")
    bundle = _json_bundle(
        [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [{"id": 1, "side": "mine", "text": "", "image": data_url}],
                    }
                ]
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text
    assert resp.json()["images"] == 1


def test_import_zip_prompt_override_is_kept(client: TestClient, device: tuple[str, str]) -> None:
    """用户自己改过的角色提示词不能丢：落到 device_prompts，而不是全局提示词表。"""
    _, token = device
    bundle = _zip_bytes(
        {
            "project.json": json.dumps(_project_json(), ensure_ascii=False).encode("utf-8"),
            "cards/00/conversations/00-伊冯.json": json.dumps(
                {"name": "伊冯", "messages": [{"side": "other", "text": "嗯"}]},
                ensure_ascii=False,
            ).encode("utf-8"),
            "prompts/characters/伊冯.txt": "我自己的伊冯，说话更简短。".encode(),
        }
    )

    resp = _post_package(client, token, bundle)
    assert resp.status_code == 201, resp.text

    from app.db import SessionLocal
    from app.repos import characters as characters_repo
    from app.repos import content as content_repo

    with SessionLocal() as db:
        character = characters_repo.find_by_slug(db, "yifeng")
        assert character is not None
        device_id = _device_id_of(db, token)
        assert content_repo.get_character_prompt(db, character.id, device_id) == (
            "我自己的伊冯，说话更简短。"
        )
        # 全局版本没被动过
        assert content_repo.get_active_prompt(db, character.id) != "我自己的伊冯，说话更简短。"


# ---------------------------------------------------------------- 语义
def test_import_unknown_character_still_keeps_history(
    client: TestClient, device: tuple[str, str]
) -> None:
    """认不出的角色照样建会话：历史留着，只是没有那个人的人设。"""
    _, token = device
    bundle = _json_bundle(
        [
            {
                "conversations": [
                    {
                        "name": "路人甲",
                        "messages": [
                            {"id": 1, "side": "mine", "text": "你是谁"},
                            {"id": 2, "side": "other", "text": "路过的"},
                        ],
                    }
                ]
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["messages"] == 2
    assert any("路人甲" in note for note in body["notes"])

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert conversations[0]["character_id"] is None
    assert conversations[0]["title"] == "路人甲"


def test_import_topic_line_becomes_system_side(client: TestClient, device: tuple[str, str]) -> None:
    """「新话题」提示行要显示在屏上，但不能进 AI 上下文 → side=system。"""
    _, token = device
    bundle = _json_bundle(
        [
            {
                "members": ["伊冯", "佩丽卡"],
                "conversations": [
                    {
                        "name": "群聊",
                        "messages": [
                            {"id": 1, "side": "mine", "text": "今天聊排班", "topic": True},
                            {"id": 2, "side": "other", "text": "排班我来", "speakerName": "伊冯"},
                        ],
                    }
                ],
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    messages = client.get(
        f"/v1/conversations/{conversations[0]['id']}/messages", headers=_auth(token)
    ).json()["items"]
    assert messages[0]["side"] == "system"
    assert messages[1]["side"] == "assistant"


def test_import_empty_messages_are_counted_as_skipped(
    client: TestClient, device: tuple[str, str]
) -> None:
    """空消息、错误气泡不进来，但要记一笔（否则用户以为数据丢了）。"""
    _, token = device
    bundle = _json_bundle(
        [
            {
                "conversations": [
                    {
                        "name": "伊冯",
                        "messages": [
                            {"id": 1, "side": "mine", "text": "   "},
                            {"id": 2, "side": "other", "text": "请求失败", "isError": True},
                            {"id": 3, "side": "other", "text": "在"},
                        ],
                    }
                ]
            }
        ]
    )

    resp = _post_package(client, token, bundle, filename="BAKER.json")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["messages"] == 1
    assert body["skipped"] == 2
    assert any("跳过 2" in note for note in body["notes"])


# ---------------------------------------------------------------- 幂等 / 撤销 / 覆盖
def test_same_package_twice_does_not_duplicate(
    client: TestClient, device: tuple[str, str]
) -> None:
    """同一个包传两次：第二次回上次的结果，会话不会建两遍。"""
    _, token = device
    bundle = _json_bundle(
        [{"conversations": [{"name": "伊冯", "messages": [{"side": "mine", "text": "在吗"}]}]}]
    )

    first = _post_package(client, token, bundle, filename="BAKER.json")
    assert first.status_code == 201
    second = _post_package(client, token, bundle, filename="BAKER.json")
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert len(conversations) == 1


def test_revoke_import_only_removes_imported_conversations(
    client: TestClient, device: tuple[str, str]
) -> None:
    """撤销导入只删这次导入建的会话，用户自己新建的一条都不动。"""
    _, token = device
    # 先手动建一条（模拟用户自己聊的）
    slug = client.get("/v1/characters").json()["items"][0]["id"]
    manual = client.post(
        "/v1/conversations", headers=_auth(token), json={"character_id": slug}
    )
    assert manual.status_code == 201
    manual_id = manual.json()["id"]

    bundle = _json_bundle(
        [{"conversations": [{"name": "伊冯", "messages": [{"side": "mine", "text": "在吗"}]}]}]
    )
    imported = _post_package(client, token, bundle, filename="BAKER.json")
    assert imported.status_code == 201
    batch_id = imported.json()["id"]

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert len(conversations) == 2

    revoked = client.delete(f"/v1/imports/{batch_id}", headers=_auth(token))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    left = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert [item["id"] for item in left] == [manual_id]


def test_replace_mode_drops_previous_conversations(
    client: TestClient, device: tuple[str, str]
) -> None:
    """覆盖模式 = 老客户端"覆盖导入"的语义：先清空，再导入。"""
    _, token = device
    slug = client.get("/v1/characters").json()["items"][0]["id"]
    assert (
        client.post("/v1/conversations", headers=_auth(token), json={"character_id": slug})
        .status_code
        == 201
    )

    bundle = _json_bundle(
        [{"conversations": [{"name": "伊冯", "messages": [{"side": "mine", "text": "在吗"}]}]}]
    )
    resp = _post_package(client, token, bundle, filename="BAKER.json", mode="replace")
    assert resp.status_code == 201, resp.text

    conversations = client.get("/v1/conversations", headers=_auth(token)).json()["items"]
    assert len(conversations) == 1
    assert conversations[0]["title"] == "伊冯"


# ---------------------------------------------------------------- 错误处理
@pytest.mark.parametrize(
    ("payload", "filename"),
    [
        (b"this is not a package", "BAKER.json"),
        (b'{"version": 1}', "BAKER.json"),  # 没有 cards
        (_zip_bytes({"cards/00/conversations/00-伊冯.json": b"{}"}), "BAKER.zip"),  # 缺 project.json
    ],
)
def test_broken_package_is_rejected(
    client: TestClient, device: tuple[str, str], payload: bytes, filename: str
) -> None:
    """坏包要明确报错（400 + 中文原因），而且一条数据都不许进来。"""
    _, token = device
    resp = _post_package(client, token, payload, filename=filename)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_package"
    assert client.get("/v1/conversations", headers=_auth(token)).json()["items"] == []


def test_import_requires_token(client: TestClient) -> None:
    """没 token 不许导 —— 数据要挂到某台设备名下，匿名的导进来属于谁？"""
    resp = client.post(
        "/v1/imports",
        files={"file": ("BAKER.json", b"{}", "application/json")},
        data={"mode": "merge"},
    )
    assert resp.status_code == 401


def test_import_rejects_unknown_mode(client: TestClient, device: tuple[str, str]) -> None:
    _, token = device
    bundle = _json_bundle(
        [{"conversations": [{"name": "伊冯", "messages": [{"side": "mine", "text": "在"}]}]}]
    )
    resp = _post_package(client, token, bundle, filename="BAKER.json", mode="mergeee")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_import_list_shows_history(client: TestClient, device: tuple[str, str]) -> None:
    _, token = device
    bundle = _json_bundle(
        [{"conversations": [{"name": "伊冯", "messages": [{"side": "mine", "text": "在"}]}]}]
    )
    assert _post_package(client, token, bundle, filename="BAKER.json").status_code == 201

    rows = client.get("/v1/imports", headers=_auth(token)).json()["items"]
    assert len(rows) == 1
    assert rows[0]["messages"] == 1


# ---------------------------------------------------------------- 小工具
def _device_id_of(db: Session, token: str) -> int:
    """测试里需要设备主键（仓库层函数收 int，不收 token）。"""
    from app.ids import sha256_hex
    from app.repos import devices as devices_repo

    device = devices_repo.find_by_token_hash(db, sha256_hex(token))
    assert device is not None
    return device.id
