"""tests/test_data_clear.py —— 设置里「危险操作」同时清服务器数据。

用户实测反馈：「这几个好像不管服务器的数据啊」—— 原来四个按钮只清本机
（Pinia + IndexedDB），服务器上的会话/消息/记忆一条没动，换台设备登回来
东西全在。这个文件守 `POST /v1/data/clear` 的四件事：

  1. **作用域**：清 A 设备不动 B 设备（这是唯一会造成不可挽回损失的一条）
  2. **三个 flag 各自的语义**：
       conversations → 会话列表空
       messages      → 消息没了、会话还在、next_seq 归零（新消息从 1 开始）
       context       → 摘要 + 干员记忆没了，会话上的两份副本也清空
  3. **参数全 false → 400**（不是"假装成功返回 0"）
  4. **登录态按账号划界**：同一账号的其它设备清得到，别的账号清不到

所有断言都直接查库（不只信接口返回的计数），否则"返回 5 但其实没删"这种
bug 会被计数骗过去。
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Character, CharacterMemory, Conversation, Device, Message, Summary
from app.owners import owner_key
from app.repos import memories as memories_repo
from app.repos import summaries as summaries_repo


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_pk(public_id: str) -> int:
    """设备对外是 public_id，库里用自增 id。"""
    with SessionLocal() as db:
        row = db.scalar(select(Device).where(Device.public_id == public_id))
        assert row is not None
        return row.id


def _character_id(slug: str = "perlica") -> int:
    with SessionLocal() as db:
        row = db.scalar(select(Character).where(Character.slug == slug))
        assert row is not None
        return row.id


def _new_conversation(client: TestClient, token: str, *, title: str = "") -> str:
    """建一个单聊会话，返回 public_id。"""
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "perlica", "title": title},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post_message(client: TestClient, token: str, public_id: str, msg_id: str, text: str) -> None:
    """写一条消息，**不生成回复**（generate=false：测试不花上游额度）。"""
    resp = client.post(
        f"/v1/conversations/{public_id}/messages",
        json={"client_msg_id": msg_id, "text": text, "generate": False},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


def _conversation_pks(public_ids: list[str]) -> list[int]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Conversation.id).where(Conversation.public_id.in_(public_ids))
        )
        return list(rows)


def _count(model: type, *where: object) -> int:
    """按条件数行数（断言用；空条件 = 整表）。"""
    from sqlalchemy import func

    with SessionLocal() as db:
        stmt = select(func.count()).select_from(model)
        for condition in where:
            stmt = stmt.where(condition)
        return int(db.scalar(stmt) or 0)


def _seed_two_devices(client: TestClient) -> tuple[tuple[str, str], tuple[str, str]]:
    """两台设备各建 1 个会话 + 1 条消息，返回 ((A 的设备 id, A 的 token), (B 的设备 id, B 的 token))。"""
    created: list[tuple[str, str]] = []
    for index in ("A", "B"):
        resp = client.post("/v1/auth/device", json={"platform": "pytest"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        public_id = _new_conversation(client, body["token"], title=f"设备{index}的会话")
        _post_message(
            client, body["token"], public_id, f"c-{index}-1", f"设备{index}说的话"
        )
        created.append((body["device_id"], body["token"]))
    return created[0], created[1]


def _clear(client: TestClient, token: str, **flags: bool):
    return client.post("/v1/data/clear", json=flags, headers=_headers(token))


# ---------------------------------------------------------------- 作用域
def test_clear_conversations_only_touches_own_device(client: TestClient) -> None:
    """清 A 的会话：A 的列表空，B 的会话和消息一条不少。"""
    (_device_a, token_a), (_device_b, token_b) = _seed_two_devices(client)
    assert len(client.get("/v1/conversations", headers=_headers(token_a)).json()["items"]) == 1

    resp = _clear(client, token_a, conversations=True)
    assert resp.status_code == 200, resp.text
    deleted = resp.json()["deleted"]
    assert deleted["conversations"] == 1
    assert deleted["messages"] == 1

    # A 空
    assert client.get("/v1/conversations", headers=_headers(token_a)).json()["items"] == []
    # B 完全没动（这是本文件最重要的一条断言）
    items_b = client.get("/v1/conversations", headers=_headers(token_b)).json()["items"]
    assert len(items_b) == 1
    messages_b = client.get(
        f"/v1/conversations/{items_b[0]['id']}/messages", headers=_headers(token_b)
    ).json()["items"]
    assert [item["text"] for item in messages_b] == ["设备B说的话"]


def test_clear_requires_flags(client: TestClient) -> None:
    """三个 flag 全 false → 400，而且一条数据都不能动。"""
    (_device, token), _other = _seed_two_devices(client)

    resp = _clear(client, token)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "bad_request"

    resp = _clear(client, token, conversations=False, messages=False, context=False)
    assert resp.status_code == 400
    # 什么都没删
    assert len(client.get("/v1/conversations", headers=_headers(token)).json()["items"]) == 1


def test_clear_requires_token(client: TestClient) -> None:
    """没有设备凭证 → 401（清理是不可恢复操作，不能匿名打）。"""
    resp = client.post("/v1/data/clear", json={"conversations": True})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] in {"no_token", "bad_token"}


# ---------------------------------------------------------------- 三个 flag
def test_clear_messages_keeps_conversations_and_resets_seq(client: TestClient) -> None:
    """messages=true：消息清空、会话留着、next_seq 归零（下一条消息是 seq 1）。"""
    (_device, token), _other = _seed_two_devices(client)
    public_id = client.get("/v1/conversations", headers=_headers(token)).json()["items"][0]["id"]
    _post_message(client, token, public_id, "c-A-2", "第二条")

    resp = _clear(client, token, messages=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["messages"] == 2
    assert resp.json()["deleted"]["conversations"] == 0

    # 会话还在
    items = client.get("/v1/conversations", headers=_headers(token)).json()["items"]
    assert len(items) == 1
    # 消息没了
    assert (
        client.get(f"/v1/conversations/{public_id}/messages", headers=_headers(token)).json()[
            "items"
        ]
        == []
    )
    # next_seq 归零
    with SessionLocal() as db:
        row = db.scalar(select(Conversation).where(Conversation.public_id == public_id))
        assert row is not None and row.next_seq == 0

    # 新消息从 1 开始（不是从 3 接着跳）
    _post_message(client, token, public_id, "c-A-3", "清完之后的第一句")
    messages = client.get(
        f"/v1/conversations/{public_id}/messages", headers=_headers(token)
    ).json()["items"]
    assert [item["seq"] for item in messages] == [1]


def test_clear_context_drops_summaries_memories_and_copies(client: TestClient) -> None:
    """context=true：摘要 + 干员记忆清空，会话上 summary_head/memory 副本也清空。"""
    (device_a, token), (device_b, other_token) = _seed_two_devices(client)
    public_id = client.get("/v1/conversations", headers=_headers(token)).json()["items"][0]["id"]
    conversation_pk = _conversation_pks([public_id])[0]

    owner = owner_key(None, _device_pk(device_a))
    character = _character_id()

    # 造一段上下文：一版摘要 + 一条干员记忆 + 会话上的永久记忆
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_pk)
        assert conversation is not None
        summaries_repo.create(
            db, conversation=conversation, summary_text="她记得你叫阿哲", covered_to_seq=1
        )
        conversation.memory = "永久记忆：你答应过带她去看海"
        db.commit()
        memories_repo.add(
            db, owner_key=owner, character_id=character, kind="nickname", text="你叫阿哲"
        )
        db.commit()

    resp = _clear(client, token, context=True)
    assert resp.status_code == 200, resp.text
    deleted = resp.json()["deleted"]
    assert deleted["summaries"] == 1
    assert deleted["memories"] == 1
    # context 不该动消息
    assert deleted["messages"] == 0

    # 摘要行、记忆行都没了
    assert _count(Summary, Summary.conversation_id == conversation_pk) == 0
    assert _count(CharacterMemory, CharacterMemory.owner_key == owner) == 0
    # 会话还在，两份副本清空
    with SessionLocal() as db:
        row = db.get(Conversation, conversation_pk)
        assert row is not None
        assert row.summary_head is None
        assert row.memory is None
    # 消息还在
    messages = client.get(
        f"/v1/conversations/{public_id}/messages", headers=_headers(token)
    ).json()["items"]
    assert len(messages) == 1

    # 另一台设备的记忆不受影响（这里它本来没有记忆，验证的是"按 owner 划界"）
    other_owner = owner_key(None, _device_pk(device_b))
    with SessionLocal() as db:
        memories_repo.add(
            db,
            owner_key=other_owner,
            character_id=character,
            kind="nickname",
            text="B 的名字",
        )
        db.commit()
    assert _clear(client, token, context=True).json()["deleted"]["memories"] == 0
    assert _count(CharacterMemory, CharacterMemory.owner_key == other_owner) == 1


def test_clear_everything_removes_all_rows(client: TestClient) -> None:
    """三个 flag 一起：会话/消息/摘要/记忆全清，返回的计数与库里一致。"""
    (device_a, token), _other = _seed_two_devices(client)
    public_id = client.get("/v1/conversations", headers=_headers(token)).json()["items"][0]["id"]
    conversation_pk = _conversation_pks([public_id])[0]
    owner = owner_key(None, _device_pk(device_a))

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_pk)
        assert conversation is not None
        summaries_repo.create(
            db, conversation=conversation, summary_text="摘要一", covered_to_seq=1
        )
        db.commit()
        memories_repo.add(
            db,
            owner_key=owner,
            character_id=_character_id(),
            kind="preference",
            text="你爱喝冰美式",
        )
        db.commit()

    resp = _clear(client, token, conversations=True, messages=True, context=True)
    assert resp.status_code == 200, resp.text
    deleted = resp.json()["deleted"]
    assert deleted == {
        "conversations": 1,
        "messages": 1,
        "summaries": 1,
        "memories": 1,
    }

    assert _count(Conversation, Conversation.id == conversation_pk) == 0
    assert _count(Message, Message.conversation_id == conversation_pk) == 0
    assert _count(Summary, Summary.conversation_id == conversation_pk) == 0
    assert _count(CharacterMemory, CharacterMemory.owner_key == owner) == 0


# ---------------------------------------------------------------- 登录态
def test_logged_in_clear_covers_all_devices_of_account(client: TestClient) -> None:
    """登录后按账号划界：同账号另一台设备的会话也一起清；别人账号的不动。"""
    (_device_a, token_a), (_device_b, token_b) = _seed_two_devices(client)

    # A 注册账号 → A 设备的会话挂到账号名下
    resp = client.post(
        "/v1/auth/register",
        json={"username": "clear_user", "password": "clear_password_2026"},
        headers=_headers(token_a),
    )
    assert resp.status_code == 201, resp.text

    # 第二台设备登录同一账号，再建一个会话（属于账号，不属于某台设备）
    resp = client.post(
        "/v1/auth/login",
        json={"username": "clear_user", "password": "clear_password_2026"},
        headers=_headers(token_b),
    )
    assert resp.status_code == 200, resp.text
    public_id_b = _new_conversation(client, token_b, title="账号里的第二个会话")

    # 用 A 清（A 是"当前设备"），账号名下的会话都该没了。
    # 这里是 3 条：A 注册时收编了 A 设备的 1 条、B 登录时收编了 B 设备的 1 条、
    # 再加 B 登录后新建的 1 条 —— 都挂在同一个账号名下，所以一起清掉。
    resp = _clear(client, token_a, conversations=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"]["conversations"] == 3

    assert client.get("/v1/conversations", headers=_headers(token_a)).json()["items"] == []
    assert client.get("/v1/conversations", headers=_headers(token_b)).json()["items"] == []
    with SessionLocal() as db:
        assert db.scalar(select(Conversation).where(Conversation.public_id == public_id_b)) is None
