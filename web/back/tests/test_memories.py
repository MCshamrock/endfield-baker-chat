"""tests/test_memories.py —— 干员记忆（人情账）与发言权重。

守五件事，每一条都对应一种"用户会当场发现被骗"的情形：
  1. 单值 kind 新写覆盖旧的 —— 不能同时相信两个名字
  2. 删掉的不许复活 —— 否则"让我忘掉"是空话
  3. 关掉开关就真的不注入，但账本还在
  4. 文本进提示词的位置在角色之后、剧情之前
  5. 群聊按权重选人、@ 优先、本轮不重复
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import group
from app.db import SessionLocal
from app.models import Character, Conversation, Device
from app.owners import owner_key
from app.prompt.dialog import build_dialog_messages
from app.repos import memories as memories_repo


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device_pk(public_id: str) -> int:
    with SessionLocal() as db:
        row = db.scalar(select(Device).where(Device.public_id == public_id))
        assert row is not None
        return row.id


def _character_id(slug: str = "perlica") -> int:
    with SessionLocal() as db:
        row = db.scalar(select(Character).where(Character.slug == slug))
        assert row is not None
        return row.id


def _new_conversation(client: TestClient, token: str, *, memory: str = "inherit") -> int:
    resp = client.post(
        "/v1/conversations",
        json={"character_id": "perlica", "memory": memory},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    public_id = resp.json()["id"]
    with SessionLocal() as db:
        row = db.scalar(select(Conversation).where(Conversation.public_id == public_id))
        assert row is not None
        return row.id


# ---------------------------------------------------------------- 账本规则
def test_single_kind_new_overrides_old(device: tuple[str, str]) -> None:
    device_id, _token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()

    with SessionLocal() as db:
        memories_repo.add(db, owner_key=owner, character_id=character, kind="nickname", text="你叫阿哲")
        memories_repo.add(db, owner_key=owner, character_id=character, kind="nickname", text="你叫老陈")
        db.commit()
        rows = memories_repo.list_active(db, owner_key=owner, character_id=character)

    texts = [row.text for row in rows]
    assert texts == ["你叫老陈"], "两个名字不能同时生效"


def test_multi_kind_coexist(device: tuple[str, str]) -> None:
    device_id, _token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()

    with SessionLocal() as db:
        memories_repo.add(db, owner_key=owner, character_id=character, kind="preference", text="你怕黑")
        memories_repo.add(db, owner_key=owner, character_id=character, kind="preference", text="你爱喝冰美式")
        db.commit()
        rows = memories_repo.list_active(db, owner_key=owner, character_id=character)

    assert len(rows) == 2


def test_same_text_is_deduped(device: tuple[str, str]) -> None:
    device_id, _token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()

    with SessionLocal() as db:
        first = memories_repo.add(db, owner_key=owner, character_id=character, kind="fact", text="你是博士")
        second = memories_repo.add(db, owner_key=owner, character_id=character, kind="fact", text="你是博士。")
        db.commit()

    assert first is not None and second is not None
    assert first.id == second.id, "标点和空白不该造出第二条"


def test_deleted_memory_is_not_resurrected(device: tuple[str, str]) -> None:
    device_id, _token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()

    with SessionLocal() as db:
        row = memories_repo.add(
            db, owner_key=owner, character_id=character, kind="promise", text="你答应带她看雪"
        )
        assert row is not None
        memories_repo.remove(db, owner_key=owner, memory_id=row.id)
        again = memories_repo.add(
            db, owner_key=owner, character_id=character, kind="promise", text="你答应带她看雪"
        )
        db.commit()

    assert again is None, "用户删过的，下次提炼不许再写回来"


def test_capacity_evicts_oldest(device: tuple[str, str]) -> None:
    device_id, _token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()

    with SessionLocal() as db:
        for index in range(memories_repo.MAX_ENTRIES + 3):
            memories_repo.add(
                db,
                owner_key=owner,
                character_id=character,
                kind="fact",
                text=f"第 {index} 条事实",
            )
        db.commit()
        rows = memories_repo.list_active(db, owner_key=owner, character_id=character)

    assert len(rows) == memories_repo.MAX_ENTRIES


# ---------------------------------------------------------------- 注入
def test_bond_layer_sits_between_character_and_summary() -> None:
    messages = build_dialog_messages(
        character_prompt="角色设定",
        bond="· 你叫阿哲",
        summary="前情",
        immersive=False,
    )
    contents = [str(item["content"]) for item in messages]
    assert any("她记得关于你的事" in text for text in contents)
    bond_index = next(i for i, t in enumerate(contents) if "她记得关于你的事" in t)
    summary_index = next(i for i, t in enumerate(contents) if "前情提要" in t)
    assert bond_index < summary_index


def test_local_time_goes_right_before_user_turn() -> None:
    messages = build_dialog_messages(
        message="晚上好",
        local_time="2026-11-16 23:20 周日",
    )
    assert messages[-1]["role"] == "user"
    assert "2026-11-16 23:20" in str(messages[-2]["content"])


def test_memory_off_hides_bond(client: TestClient, device: tuple[str, str]) -> None:
    """关掉这一局的读取：账本还在，但提示词里不许出现。"""
    from app import generate

    device_id, token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()
    with SessionLocal() as db:
        memories_repo.add(db, owner_key=owner, character_id=character, kind="nickname", text="你叫阿哲")
        db.commit()

    conversation_id = _new_conversation(client, token, memory="off")
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        character_row = db.get(Character, character)
        built = generate.build_messages(
            db, conversation, character_row, "在吗", immersive=False, before_seq=1
        )

    joined = "\n".join(str(item["content"]) for item in built)
    assert "她记得关于你的事" not in joined


# ---------------------------------------------------------------- 接口
def test_memory_api_list_delete(client: TestClient, device: tuple[str, str]) -> None:
    device_id, token = device
    owner = owner_key(None, _device_pk(device_id))
    character = _character_id()
    with SessionLocal() as db:
        memories_repo.add(db, owner_key=owner, character_id=character, kind="nickname", text="你叫阿哲")
        db.commit()

    resp = client.get("/v1/memories?character_id=perlica", headers=_headers(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and body["items"][0]["kind"] == "nickname"

    memory_id = body["items"][0]["id"]
    resp = client.delete(f"/v1/memories/{memory_id}", headers=_headers(token))
    assert resp.status_code == 204

    resp = client.get("/v1/memories?character_id=perlica", headers=_headers(token))
    assert resp.json()["total"] == 0


def test_memory_settings_roundtrip(client: TestClient, device: tuple[str, str]) -> None:
    _device_id, token = device
    assert client.get("/v1/memories/settings", headers=_headers(token)).json()["mode"] == "on"
    resp = client.put("/v1/memories/settings", json={"mode": "off"}, headers=_headers(token))
    assert resp.status_code == 200
    assert client.get("/v1/memories/settings", headers=_headers(token)).json()["mode"] == "off"


def test_weight_endpoint_clamps(client: TestClient, device: tuple[str, str]) -> None:
    _device_id, token = device
    resp = client.put(
        "/v1/characters/perlica/weight", json={"active": 100}, headers=_headers(token)
    )
    assert resp.status_code == 200 and resp.json()["active"] == 100
    out_of_range = client.put(
        "/v1/characters/perlica/weight", json={"active": 0}, headers=_headers(token)
    )
    assert out_of_range.status_code == 400


# ---------------------------------------------------------------- 选人
MEMBERS = [(1, "perlica", "佩丽卡", 0), (2, "chenqianyu", "陈千语", 1)]


def test_choose_speaker_respects_mentions() -> None:
    chosen = group.choose_speaker(
        MEMBERS, weights={}, spoken=set(), mentions=["chenqianyu"]
    )
    assert chosen is not None and chosen[1] == "陈千语"


def test_choose_speaker_skips_who_already_spoke() -> None:
    chosen = group.choose_speaker(MEMBERS, weights={}, spoken={"佩丽卡"}, mentions=None)
    assert chosen is not None and chosen[1] == "陈千语"


def test_choose_speaker_returns_none_when_all_spoke() -> None:
    assert group.choose_speaker(MEMBERS, weights={}, spoken={"佩丽卡", "陈千语"}) is None


def test_choose_speaker_is_weighted() -> None:
    """权重 100:1 时，抽 200 次应该几乎全是重的那个人。"""
    picks = [group.choose_speaker(MEMBERS, weights={1: 100, 2: 1}, spoken=set()) for _ in range(200)]
    names: Any = [item[1] for item in picks if item is not None]
    assert names.count("佩丽卡") > 180
