"""suggestions.py —— 输入推荐的业务逻辑（三个入口共用）。

一句话：**给最近几轮对话，让模型写 3 句"玩家接下来可能说的话"。**

三条边界：

1. **永远不抛异常**。推荐失败就是没有推荐；让聊天界面因为一个锦上添花的功能弹错，
   是最典型的"小功能搞坏主流程"。
2. **不记账**。它是小调用（几百 token），混进用量统计会把"每轮花了多少"搅浑。
3. **不写库**。推荐是临时的，玩家不选就消失了，不该占消息表。

代价说明：每次调用都是一次真实的上游请求。所以客户端要"玩家点开面板才拉"，
不要每轮都拉 —— 老后端就是被这个逼成按需的（每轮多一次调用，限速提前撞满）。
"""

import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session

from app import generate, upstream
from app.models import Character, Conversation
from app.prompt.suggest import SUGGEST_SYSTEM, build_material, parse_suggestions
from app.repos import messages as messages_repo

logger = logging.getLogger("baker.suggest")

# 给模型看多少条历史。推荐只需要"最近在聊什么"，给多了反而会跑题。
HISTORY_LIMIT = 12


async def suggest_for_conversation(
    db: Session, *, conversation: Conversation, character: Character | None
) -> list[str]:
    """按服务端存的历史生成推荐（新客户端走这条）。"""
    rows = messages_repo.list_before_seq(
        db, conversation.id, conversation.next_seq + 1, HISTORY_LIMIT
    )
    pairs = _pairs_of(rows)
    return await suggest_from_pairs(
        db,
        character_name=character.name if character is not None else "",
        transcript=_transcript(pairs),
    )


async def suggest_from_pairs(
    db: Session,
    *,
    character_name: str = "",
    transcript: str = "",
    identity_note: str = "",
    topic: str = "",
    members: str = "",
) -> list[str]:
    """已经有对话文本时直接生成（老客户端/老群聊走这条）。"""
    material = build_material(
        character_name=character_name,
        identity_note=identity_note,
        topic=topic,
        transcript=transcript,
        members=members,
    )
    messages: list[dict[str, object]] = [
        {"role": "system", "content": SUGGEST_SYSTEM},
        {"role": "user", "content": material},
    ]
    try:
        generation = await generate.one_shot(db, messages, immersive=False, think=False)
    except upstream.UpstreamError as exc:
        logger.warning("推荐生成失败（返回空）：%s", exc)
        return []
    return parse_suggestions(generation.text)


def _pairs_of(rows: Sequence[object]) -> list[tuple[str, str]]:
    """消息行 → [(说话人, 内容)]。用户侧统一叫"玩家"。"""
    pairs: list[tuple[str, str]] = []
    for row in rows:
        side = getattr(row, "side", "")
        text = str(getattr(row, "text", "") or "").strip()
        if not text:
            continue
        if side == "user":
            pairs.append(("玩家", text))
        else:
            pairs.append((str(getattr(row, "speaker", "") or "对方"), text))
    return pairs


def _transcript(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{who}：{text}" for who, text in pairs)


async def suggest_for_group(
    db: Session,
    *,
    members: list[str],
    transcript: str,
    identity_note: str = "",
    topic: str = "",
) -> list[str]:
    """群聊推荐：多一层"在场的人"，因为群里说话要认人。"""
    return await suggest_from_pairs(
        db,
        transcript=transcript,
        identity_note=identity_note,
        topic=topic,
        members="、".join(members),
    )


__all__ = ["HISTORY_LIMIT", "suggest_for_conversation", "suggest_for_group", "suggest_from_pairs"]
