"""tests/test_memory.py —— 摘要压缩：聊长了要"记得开头"，而且**一条都不许丢**。

守四件事：
  1. 超过预算才压（闲聊二十句不该触发）
  2. 压完保留最近几轮原文（刚说的话必须逐字在）
  3. 压过的部分不再发原文，但摘要进 prompt（"记得"而不是"塞回去"）
  4. 摘要是滚动的：第二次压缩必须把上一次的提要一起重写，否则每压一次忘一段
"""

import asyncio

import pytest
from sqlalchemy import text

from app import generate, keys, memory
from app.db import SessionLocal
from app.models import Character, Conversation, Device
from app.repos import messages as messages_repo
from app.repos import summaries as summaries_repo
from app.settings import get_settings
from app.upstream import Reply


def _make_conversation(message_count: int, text_len: int) -> tuple[int, int]:
    """造一个会话，塞进 N 条消息，返回 (conversation_pk, next_seq)。"""
    from app.clock import utcnow
    from app.ids import new_public_id

    with SessionLocal() as db:
        device = db.query(Device).first()
        if device is None:
            device = Device(
                public_id=new_public_id(),
                token_hash="x" * 64,
                platform="pytest",
                app_ver="",
                app_build=0,
                model="",
                os_ver="",
                tz="",
                locale="",
                created_at=utcnow(),
                last_seen_at=utcnow(),
            )
            db.add(device)
            db.flush()
        character = db.query(Character).first()
        conversation = Conversation(
            public_id=new_public_id(),
            device_id=device.id,
            character_id=character.id if character else None,
            mode="single",
            title="t",
            next_seq=0,
            created_at=utcnow(),
            last_active_at=utcnow(),
        )
        db.add(conversation)
        db.flush()
        for index in range(message_count):
            messages_repo.insert(
                db,
                conversation=conversation,
                client_msg_id=f"c-{index}",
                side="user" if index % 2 == 0 else "assistant",
                text="字" * text_len,
            )
        db.commit()
        return conversation.id, conversation.next_seq


@pytest.fixture
def fake_summary(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"count": 0}

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        calls["count"] += 1
        system = str(messages[0].get("content", ""))
        if "永久记忆" in system:
            # 模拟"只输出新增条目"：每次给一条新的
            return Reply(text=f"· 第 {calls['count']} 次新增的事实")
        return Reply(text="【情节纪要】管理员和佩丽卡聊了很久，约好了明天继续。" * 8)

    keys.reset()
    monkeypatch.setattr("app.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "keep_recent_turns", 2)
    return calls


def test_compresses_when_over_threshold(fake_summary: dict[str, int]) -> None:
    conversation_id, next_seq = _make_conversation(message_count=20, text_len=200)

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        # 20 条 × 200 字 ≈ 2700 token；预算 4000 → 目标 2000，必须压
        rounds = asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=4000))

        active = summaries_repo.get_active(db, conversation_id)
        assert rounds >= 1
        assert fake_summary["count"] == 3  # 摘要 + 永久记忆 + 人情账，三次调用
        assert active is not None
        # 保留最近 2 轮 = 4 条原文
        assert active.covered_to_seq == next_seq - 4
        summary_head = db.execute(
            text("SELECT summary_head, memory FROM conversations WHERE id = :i"),
            {"i": conversation_id},
        ).first()
        assert summary_head is not None
        assert summary_head[0] and "情节纪要" in summary_head[0]
        # 永久记忆：只追加的新条目，以「·」开头
        assert summary_head[1] and summary_head[1].startswith("·")


def test_memory_is_append_only(fake_summary: dict[str, int]) -> None:
    """永久记忆只追加：已有条目必须原样保留，绝不允许被"重新压缩"删掉。

    这是 prompt 级的要求做不到的事 —— 模型每次都被允许"合并"，就一定会删。
    所以这里从数据层验：第二次压缩后，第一次写进去的那条还在。
    """
    conversation_id, next_seq = _make_conversation(message_count=20, text_len=200)

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None

        asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=4000))
        first = conversation.memory or ""
        assert first.startswith("·")

        # 造第二批历史，再压一次
        for index in range(20):
            messages_repo.insert(
                db,
                conversation=conversation,
                client_msg_id=f"d-{index}",
                side="user" if index % 2 == 0 else "assistant",
                text="字" * 200,
            )
        db.commit()
        asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=4000))

        second = conversation.memory or ""
    assert first in second, "第一次的条目被覆盖/删掉了 —— 这就不是只增不减了"
    assert second.count("·") > first.count("·")


def test_skips_when_short(fake_summary: dict[str, int]) -> None:
    """闲聊几句不该触发压缩（压缩会多打一次上游，也白写库）。"""
    conversation_id, _ = _make_conversation(message_count=4, text_len=10)
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        rounds = asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=200_000))
        assert rounds == 0
        assert fake_summary["count"] == 0
        assert summaries_repo.get_active(db, conversation_id) is None


def test_prompt_uses_summary_and_drops_covered(fake_summary: dict[str, int]) -> None:
    """压缩之后：摘要进 system，被覆盖的原文不再进 prompt。"""
    conversation_id, next_seq = _make_conversation(message_count=20, text_len=200)
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        character = db.get(Character, conversation.character_id) if conversation.character_id else None
        __import__("asyncio").run(
            memory.compress_to_fit(db, conversation, budget_tokens=4000)
        )
        db.refresh(conversation)

        built = generate.build_messages(
            db, conversation, character, "新的一句话", immersive=False, before_seq=next_seq + 1
        )

    systems = [m["content"] for m in built if m["role"] == "system"]
    joined = "\n".join(str(item) for item in systems)
    assert "前情提要" in joined
    assert "永久记忆" in joined
    # 永久记忆排在前情提要之后（更稳定的越靠前）
    assert joined.index("前情提要") < joined.index("永久记忆")

    # 被覆盖的老消息不该还在 prompt 里：只剩"保留的最近几轮 + 本轮这条"
    history_texts = [m["content"] for m in built if m["role"] in ("user", "assistant")]
    assert len(history_texts) <= 5  # 4 条保留 + 本轮输入
    assert history_texts[-1] == "新的一句话"


def test_rolling_summary_merges_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二次压缩必须把上一次的提要一起重写 —— 否则每压一次就忘掉上一段（滚动 vs 替换）。"""
    materials: list[str] = []

    async def _complete(
        messages: list[dict[str, object]],
        *,
        credential: object = None,
        think: bool = False,
        **_: object,
    ) -> Reply:
        system = str(messages[0].get("content", ""))
        if "永久记忆" in system or "她记得" in system:
            return Reply(text="无")
        materials.append(str(messages[1].get("content", "")))
        return Reply(text="【情节纪要】" + "管理员和佩丽卡聊了很久。" * 20)

    keys.reset()
    monkeypatch.setattr("app.upstream.complete", _complete)
    monkeypatch.setattr(get_settings(), "upstream_api_key", "sk-test")
    monkeypatch.setattr(get_settings(), "keep_recent_turns", 1)

    conversation_id, _ = _make_conversation(message_count=20, text_len=200)
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=3000))
        first_head = conversation.summary_head or ""
        assert first_head

        # 再来一批，压第二次
        for index in range(10):
            messages_repo.insert(
                db,
                conversation=conversation,
                client_msg_id=f"r-{index}",
                side="user" if index % 2 == 0 else "assistant",
                text="字" * 200,
            )
        db.commit()
        asyncio.run(memory.compress_to_fit(db, conversation, budget_tokens=3000))

    assert len(materials) >= 2
    # 第一次是首压：直接给原文；第二次必须带上"已有的前情提要"
    assert "已有的前情提要" not in materials[0]
    assert "已有的前情提要" in materials[1]


def test_long_history_is_not_truncated(fake_summary: dict[str, int]) -> None:
    """预算够的时候，历史必须**原样全带**：老版本那个 50 条硬顶不许回来。"""
    conversation_id, next_seq = _make_conversation(message_count=120, text_len=5)
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        assert conversation is not None
        character = (
            db.get(Character, conversation.character_id) if conversation.character_id else None
        )
        built = generate.build_messages(
            db, conversation, character, "这一句", immersive=False, before_seq=next_seq + 1
        )

    history_texts = [m["content"] for m in built if m["role"] in ("user", "assistant")]
    assert len(history_texts) == 121  # 120 条历史 + 本轮
    assert fake_summary["count"] == 0  # 没超预算就不该压
