"""tests/test_prompt_v2.py —— 老客户端的「使用新提示词」开关（characters_v2 长卡）。

守六件事，每一条都对应一个真实会坏的场景：

1. **逐字节一致**：导入脚本把老 JSON 的 prompt 原样写进库。少一个换行、
   多一次 strip，都算改人设 —— 所以这里比的是字节，不是"大致相同"。
2. **v1 / v2 确实是两套**：两套正文必须不同（否则开关等于没接）。
3. **单聊生效**：`use_new_prompt=True` 时，legacy 单聊拼提示词拿到的必须是 v2。
4. **回落**：某个角色没有 v2 时，取到的是 v1 正文，不是空人设。
5. **群聊生效**：一次 round 里每个发言人都按同一个 variant 取词。
6. **不串味**：先取 v1 再取 v2，两边内容不互相污染。

测试库：conftest 会把 `BAKER_DATABASE_URL` 的库名加 `_test` 后缀，
所以跑之前要指到自己的库（见任务里的命令），不要指到同事的 baker_back。
"""

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import generate
from app.api import legacy_group
from app.db import SessionLocal
from app.models import Character, CharacterPrompt
from app.repos import content as content_repo
from scripts.import_prompts_v2 import DEFAULT_SOURCE_DIR, import_all


# ---------------------------------------------------------------- 小工具
def _character(db: Session, slug: str) -> Character:
    character = db.scalar(select(Character).where(Character.slug == slug))
    assert character is not None, f"测试库的 characters 表里没有 {slug}"
    return character


def _stored_body(db: Session, slug: str, variant: str) -> str:
    """直接从库里取某个角色某套变体的正文（不经过缓存/仓库层）。"""
    character = _character(db, slug)
    body = db.scalar(
        select(CharacterPrompt.body)
        .where(
            CharacterPrompt.character_id == character.id,
            CharacterPrompt.variant == variant,
            CharacterPrompt.active == 1,
        )
        .order_by(CharacterPrompt.ver.desc())
        .limit(1)
    )
    assert body is not None, f"{slug} 没有 {variant} 正文"
    return body


def _old_pack_prompt(slug: str) -> str:
    """直接读老包 JSON，作为"标准答案"。json.loads 会把 \\n 还原成真实换行。"""
    path = DEFAULT_SOURCE_DIR / f"{slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data["prompt"])


def _delete_v2_rows(db: Session, slug: str) -> None:
    """删掉某个角色的 v2 行（用来模拟"这个角色还没写 v2"）。"""
    character = _character(db, slug)
    db.query(CharacterPrompt).filter(
        CharacterPrompt.character_id == character.id,
        CharacterPrompt.variant == "v2",
    ).delete()
    db.commit()


def _count_v2_rows(db: Session) -> int:
    return int(
        db.query(CharacterPrompt).filter(CharacterPrompt.variant == "v2").count()
    )


@pytest.fixture(autouse=True)
def imported_v2() -> None:
    """每个用例前确保 v2 已经导入。

    导入脚本本身是幂等的（按 character_id + variant 更新），重复跑只是把同一批
    行再写一遍，所以可以在每个用例前跑一次，而不用关心测试之间的先后顺序。
    """
    legacy_group._SESSIONS.clear()
    content_repo.clear_cache()
    with SessionLocal() as db:
        stats = import_all(db, DEFAULT_SOURCE_DIR)
    assert stats["files"] == 36, f"v2 角色文件数不对：{stats}"
    assert stats["no_character"] == 0, f"有角色在 characters 表里找不到：{stats}"


def _single_payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "character_id": "perlica",
        "message": "在吗",
        "history": [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早，管理员。"},
        ],
        "immersive_mode": False,
    }
    payload.update(extra)
    return payload


def _group_history() -> list[dict[str, str]]:
    return [
        {"side": "other", "text": "今天天气不错。", "speaker": "佩丽卡"},
        {"side": "mine", "text": "你们怎么都不说话", "speaker": ""},
    ]


# ---------------------------------------------------------------- 1. 逐字节导入
@pytest.mark.parametrize("slug", ["perlica", "lifeng", "endministrator"])
def test_v2_body_is_byte_identical_to_old_pack(slug: str) -> None:
    """库里 v2 正文必须和老 JSON 的 prompt **逐字节**相等。

    同一份内容既比 str（字符级）也比 encode 之后的 bytes（字节级）：
    前者能抓"多了个空格"，后者能抓"编码被换过"。
    """
    old = _old_pack_prompt(slug)
    with SessionLocal() as db:
        stored = _stored_body(db, slug, "v2")

    assert stored == old
    assert stored.encode("utf-8") == old.encode("utf-8")
    # 老包正文是真实换行（不是字面量 "\n"）：段落之间应该有大量换行
    assert stored.count("\n") > 10


# ---------------------------------------------------------------- 2. 两套确实不同
def test_v2_differs_from_v1() -> None:
    """v1 / v2 必须真的是两套内容，长度也要不同（长度不同 = 最省事的证明）。"""
    for slug in ("perlica", "lifeng"):
        with SessionLocal() as db:
            v1 = _stored_body(db, slug, "v1")
            v2 = _stored_body(db, slug, "v2")
        assert v1, f"{slug} 的 v1 是空的"
        assert v2, f"{slug} 的 v2 是空的"
        assert v1 != v2, f"{slug} 的 v1/v2 内容相同，开关等于没接"
        assert len(v1) != len(v2), f"{slug} 的 v1/v2 长度相同，不像两套提示词"


# ---------------------------------------------------------------- 3. 单聊开关
def test_legacy_single_chat_uses_v2_when_switch_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`use_new_prompt=True` 时，单聊拿到的是 v2 正文。

    做法：拦下 `build_dialog_messages` 的实参，看它收到的 character_prompt 是什么。
    这样不用真的调上游，也不会因为网络/密钥导致测试飘。
    """
    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"role": "user", "content": "占位"}]

    async def _fake_one_shot(
        db: Any, messages: Any, *, immersive: bool, think: bool
    ) -> Any:
        return generate.Generation(text="嗯。")

    monkeypatch.setattr("app.api.legacy.build_dialog_messages", _fake_build)
    monkeypatch.setattr("app.api.legacy.generate.one_shot", _fake_one_shot)

    resp = client.post("/chat", json=_single_payload(use_new_prompt=True))
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        expected = _stored_body(db, "perlica", "v2")
    assert captured["character_prompt"] == expected


def test_legacy_single_chat_keeps_v1_when_switch_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关着（老客户端默认）时还是 v1 —— 这条防的是"改 v2 把老路径带坏了"。"""
    captured: dict[str, Any] = {}

    def _fake_build(**kwargs: Any) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"role": "user", "content": "占位"}]

    async def _fake_one_shot(
        db: Any, messages: Any, *, immersive: bool, think: bool
    ) -> Any:
        return generate.Generation(text="嗯。")

    monkeypatch.setattr("app.api.legacy.build_dialog_messages", _fake_build)
    monkeypatch.setattr("app.api.legacy.generate.one_shot", _fake_one_shot)

    resp = client.post("/chat", json=_single_payload())
    assert resp.status_code == 200, resp.text

    with SessionLocal() as db:
        expected = _stored_body(db, "perlica", "v1")
    assert captured["character_prompt"] == expected


# ---------------------------------------------------------------- 4. v2 缺失回落 v1
def test_missing_v2_falls_back_to_v1(caplog: pytest.LogCaptureFixture) -> None:
    """某个角色没写 v2 时，取 v2 必须拿到 v1 正文（而不是空串）。"""
    with SessionLocal() as db:
        v1 = _stored_body(db, "qiuli", "v1")
        _delete_v2_rows(db, "qiuli")
        content_repo.clear_cache()

        caplog.set_level(logging.DEBUG, logger="baker.content")
        body = content_repo.get_active_prompt(db, _character(db, "qiuli").id, "v2")

    assert body == v1
    assert "回落" in caplog.text


# ---------------------------------------------------------------- 5. 群聊开关
def test_legacy_group_uses_v2_for_every_speaker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """群聊 round 一次给全员各一条：每个发言人都要用请求里的同一个 variant。"""
    captured: list[dict[str, Any]] = []

    def _fake_build(**kwargs: Any) -> list[dict[str, object]]:
        captured.append(kwargs)
        return [{"role": "user", "content": "占位"}]

    async def _fake_upstream_call(
        db: Any, messages: Any, *, immersive: bool, think: bool
    ) -> Any:
        return generate.Generation(text="（点头）")

    monkeypatch.setattr("app.api.legacy_group.build_group_messages", _fake_build)
    monkeypatch.setattr("app.api.legacy_group.upstream_call", _fake_upstream_call)

    created = client.post(
        "/group/session",
        json={"members": ["佩丽卡", "陈千语"], "speak_mode": "round", "my_role": "admin"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    resp = client.post(
        "/group/reply",
        json={
            "session_id": session_id,
            "history": _group_history(),
            "use_new_prompt": True,
            "want_suggestions": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["speaker"] for item in body["replies"]] == ["佩丽卡", "陈千语"]

    with SessionLocal() as db:
        expected = {
            "佩丽卡": _stored_body(db, "perlica", "v2"),
            "陈千语": _stored_body(db, "chenqianyu", "v2"),
        }
    picked = {item["speaker"]: item["character_prompt"] for item in captured}
    assert picked == expected


# ---------------------------------------------------------------- 6. 缓存隔离
def test_v1_and_v2_do_not_mix() -> None:
    """先取 v1 再取 v2（再切回 v1），三次数值都要对得上自己那套。"""
    with SessionLocal() as db:
        character = _character(db, "perlica")
        stored_v1 = _stored_body(db, "perlica", "v1")
        stored_v2 = _stored_body(db, "perlica", "v2")

        content_repo.clear_cache()
        v1 = content_repo.get_active_prompt(db, character.id, "v1")
        v2 = content_repo.get_active_prompt(db, character.id, "v2")
        v1_again = content_repo.get_active_prompt(db, character.id, "v1")
        # 不传 variant = 老路径的默认行为，必须是 v1
        default = content_repo.get_active_prompt(db, character.id)

    assert v1 == stored_v1
    assert v2 == stored_v2
    assert v1_again == stored_v1
    assert default == stored_v1
    assert v1 != v2


# ---------------------------------------------------------------- 7. 导入脚本本身
def test_import_is_idempotent_and_dry_run_does_not_write() -> None:
    """幂等 + --dry-run 不写库：这两个是"敢在生产上重跑"的前提。"""
    with SessionLocal() as db:
        before = _count_v2_rows(db)
        _delete_v2_rows(db, "si")
        assert _count_v2_rows(db) == before - 1

        dry = import_all(db, DEFAULT_SOURCE_DIR, dry_run=True)
        assert dry["inserted"] == 1
        assert _count_v2_rows(db) == before - 1  # 试运行一行都没写

        real = import_all(db, DEFAULT_SOURCE_DIR)
        assert real["inserted"] == 1
        assert _count_v2_rows(db) == before

        again = import_all(db, DEFAULT_SOURCE_DIR)
        assert again["inserted"] == 0
        assert again["updated"] == 36
        assert _count_v2_rows(db) == before
