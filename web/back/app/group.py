"""group.py —— 群聊编排：这一轮谁说话，以及替他说。

四种发言策略（`conversations.speak_mode`）：

  * `weighted` **默认**。按 active 权重加权随机，本轮说过的人不再抽（无放回）；
               被 @ 的人优先，几个人都被 @ 就在他们之间加权随机。
               不花额外的上游调用，也不依赖模型判断 —— 这是新版群聊的主路径。
  * `round`    按成员顺序轮转。最省、最可预测，适合"我就想听她们聊"。
  * `smart`    额外问一次"主持人"谁最该接话（一次很小的上游调用），
               他可以回 `END` 表示这个话题该停了。
  * `assign`   由玩家指定这一次谁说话。

权重从哪来：`characters.active_weight`（每个干员天生不同）→ 玩家在
`character_weights` 里可以覆盖。见 docs/04、docs/08 §2.1。

一条铁律：**一次请求只产生一条角色发言**。
"直到所有人说完"是客户端做的事 —— 发一句 → 收一条 → 等两秒 → 再请求一次，
每轮换一个人，直到池子空了（`/v1/.../next` 会回 `done=true`）。
服务端不自己连发，因为那样会占着连接十几秒，用户点停止也停不下来。

`END` 的处理也很重要：主持人说停就真的不发言（这段对话聊完了），
不要为了"有回复"硬让某个人说一句 —— 那是群聊里最明显的假。
"""

import json
import logging
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import keys, memory, upstream
from app.generate import Generation, _inline, load_bond
from app.models import Character, Conversation, Message
from app.owners import owner_key
from app.prompt.group import JUDGE_SYSTEM, build_group_messages, build_transcript
from app.repos import content as content_repo
from app.repos import memories as memories_repo
from app.repos import messages as messages_repo
from app.repos import summaries as summaries_repo
from app.settings import get_settings

logger = logging.getLogger("baker.group")

# 群聊提示词里"除群聊记录之外"的那部分占多少 token（协议/世界观/群规/人设/提要/永久记忆/检索）。
# 只是个估算值：算出偏差只影响压缩时机，不影响"能不能装下"。
GROUP_PREFIX_RESERVE_TOKENS = 12_000


async def pick_speaker(
    db: Session,
    *,
    members: list[tuple[int, str, str, int]],
    transcript: str,
    last_speaker: str,
) -> tuple[str, str] | None:
    """问主持人："下一条谁发？"

    返回 (slug, 显示名)；应当停止时返回 None。
    主持人调用失败时**退回轮转**——不能让一次小调用把整个群聊卡死。
    """
    names = [name for _cid, _slug, name, _ord in members]
    if not names:
        return None
    if len(names) == 1:
        return members[0][1], members[0][2]

    material = (
        f"【群成员】{'、'.join(names)}\n"
        f"【上一条是谁说的】{last_speaker or '（还没有人发言）'}\n"
        f"【最近的群聊记录】\n{transcript[-4000:]}"
    )
    try:
        reply = await _small_call(db, JUDGE_SYSTEM, material, max_tokens=120)
    except upstream.UpstreamError as exc:
        logger.warning("主持人判断失败，退回轮转：%s", exc)
        return _round_next(members, last_speaker)

    decision = _parse_decision(reply)
    if decision is None:
        return _round_next(members, last_speaker)
    if decision == "END":
        logger.info("主持人判定：这个话题该停了")
        return None

    for _cid, slug, name, _ord in members:
        if name == decision or slug == decision:
            return slug, name
    logger.warning("主持人给了一个不在群里的名字（%s），退回轮转", decision)
    return _round_next(members, last_speaker)


def choose_speaker(
    members: list[tuple[int, str, str, int]],
    *,
    weights: dict[int, int],
    spoken: set[str],
    mentions: list[str] | None = None,
) -> tuple[str, str] | None:
    """按 active 权重加权随机挑下一个发言的人。

    `spoken` 是本轮已经说过话的人（无放回）；`mentions` 是这一句里被 @ 的 slug。
    被 @ 的人优先；几个人都被 @ 时，在他们之间按权重随机。

    池子空了就返回 None —— **这一轮到此为止**，别为了"有回复"硬拉一个人出来，
    那是群聊里最明显的假。
    """
    pool = [member for member in members if member[2] not in spoken]
    if not pool:
        return None

    if mentions:
        wanted = [member for member in pool if member[1] in mentions]
        if wanted:
            pool = wanted

    picks = random.choices(pool, weights=[max(1, weights.get(m[0], 50)) for m in pool], k=1)
    chosen = picks[0]
    return chosen[1], chosen[2]


def _spoken_this_round(rows: list[Message]) -> set[str]:
    """本轮已经发过言的 AI 成员（从最近往回数，碰到玩家那条就停）。"""
    names: set[str] = set()
    for row in reversed(rows):
        if row.side == "user":
            break
        if row.side == "assistant" and row.speaker:
            names.add(row.speaker)
    return names


def _weights(
    db: Session, conversation: Conversation, members: list[tuple[int, str, str, int]]
) -> dict[int, int]:
    """每个成员的发言权重：玩家自定义的优先，否则用角色自带的 active_weight。"""
    ids = [cid for cid, _slug, _name, _ord in members]
    base = {
        row.id: row.active_weight
        for row in db.scalars(select(Character).where(Character.id.in_(ids)))
    }
    owner = owner_key(conversation.user_id, conversation.device_id)
    base.update(memories_repo.weights_for(db, owner_key=owner, character_ids=ids))
    return base


def unspoken_names(
    db: Session, conversation: Conversation, members: list[tuple[int, str, str, int]]
) -> list[str]:
    """本轮还没发言的 AI 成员（"本轮" = 最后一条玩家消息之后）。"""
    rows = messages_repo.list_after_seq(
        db, conversation.id, messages_repo.last_user_seq(db, conversation.id), 500
    )
    spoken = _spoken_this_round(rows)
    return [name for _cid, _slug, name, _ord in members if name not in spoken]


def _round_next(
    members: list[tuple[int, str, str, int]], last_speaker: str
) -> tuple[str, str]:
    """轮转：接着上一位的下一位；没有上一位就从第一个开始。"""
    for index, (_cid, slug, name, _ord) in enumerate(members):
        if name == last_speaker or slug == last_speaker:
            nxt = members[(index + 1) % len(members)]
            return nxt[1], nxt[2]
    return members[0][1], members[0][2]


def _parse_decision(text: str) -> str | None:
    """从模型输出里抠出 `next` 字段（容忍 ```json 包裹和前后废话）。"""
    raw = text.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    nxt = str(data.get("next") or "").strip()
    return nxt or None


def _mentioned_member(
    player_text: str, members: list[tuple[int, str, str, int]]
) -> tuple[str, str] | None:
    """玩家这句话里点名了谁？点名优先，不走主持人判断。

    为什么用代码判而不是让模型判：实测过 —— 玩家说「佩丽卡你先说说」，
    主持人照样把话筒给了别人（模型看到了名字，但它更愿意按"谁有话想说"来选）。
    点名是最强的意图信号，代码判一次就够，判不准的代价比漏判大得多。
    """
    text = player_text.strip()
    if not text:
        return None
    for _cid, slug, name, _ord in members:
        if name and name in text:
            return slug, name
    return None


def match_member(
    members: list[tuple[int, str, str, int]], who: str
) -> tuple[str, str] | None:
    """在群里找人：slug 和显示名都认。

    【2026-11-20 修 B1】以前只按 slug 比，而前端 `assign_speaker` 传的是**中文名**
    （明河 / 骏卫……），于是永远比不上，落到"改由主持人决定"——
    用户看到的就是"指定了 A，回话的是 B"。现在三种写法都认，老客户端不用改：

      1. slug 精确（`chenqianyu`）
      2. 显示名精确（`陈千语`）
      3. 上面两种的**不分大小写**版本（`ChenQianYu` / 前后带空格也认）

    为什么把容错放在后端而不是只改前端：老版本客户端（EXE / APK，以及缓存里的
    旧 JS）都还在用，只改前端等于"新用户好了、老用户照旧"。
    """
    key = (who or "").strip()
    if not key:
        return None
    for _cid, slug, name, _order in members:
        if slug == key or name == key:
            return slug, name
    lowered = key.casefold()
    for _cid, slug, name, _order in members:
        if slug.casefold() == lowered or (name or "").casefold() == lowered:
            return slug, name
    return None


async def _decide_speaker(
    db: Session,
    conversation: Conversation,
    members: list[tuple[int, str, str, int]],
    *,
    speaker_slug: str | None,
    mentions: list[str] | None,
    mentioned: tuple[str, str] | None,
    spoken: set[str],
    transcript: str,
    last_speaker: str,
) -> tuple[str, str] | None:
    """这一轮谁说话。优先级：指定 > 结构化 @ > 权重随机 > 文本点名 > 轮转 > 主持人。"""
    if speaker_slug:
        picked = match_member(members, speaker_slug)
        if picked is not None:
            return picked
        logger.warning("指定的发言人（%s）不在群里，改由主持人决定", speaker_slug)
        return await pick_speaker(
            db, members=members, transcript=transcript, last_speaker=last_speaker
        )

    if mentions:
        # 前端选中了谁就是谁；几个人都被 @ 时，在他们之间按权重随机
        return choose_speaker(
            members, weights=_weights(db, conversation, members), spoken=spoken, mentions=mentions
        )

    if conversation.speak_mode == "weighted":
        return choose_speaker(
            members, weights=_weights(db, conversation, members), spoken=spoken
        )

    if mentioned is not None:
        # 文本点名（老客户端没有结构化 @）：不让主持人有"发挥"的空间
        logger.info("玩家点名了「%s」，直接由她接话", mentioned[1])
        return mentioned

    if conversation.speak_mode == "round":
        return _round_next(members, last_speaker)

    return await pick_speaker(
        db, members=members, transcript=transcript, last_speaker=last_speaker
    )


async def reply_once(
    db: Session,
    *,
    conversation: Conversation,
    members: list[tuple[int, str, str, int]],
    player_seq: int,
    player_text: str,
    speaker_slug: str | None,
    mentions: list[str] | None = None,
    local_time: str = "",
    time_enabled: bool = True,
    immersive: bool,
    think: bool,
    device_id: int,
    request_id: str,
) -> tuple[str, Generation] | None:
    """决定谁说话并生成一条回复。返回 (发言人显示名, 生成结果)；不发言返回 None。"""
    # 群聊也要压缩，理由和单聊一样：不压的话，前面的剧情就永远看不到了（那是失忆，不是省地方）。
    # 群聊的固定前缀（协议/世界观/群规/人设/提要/永久记忆/检索）大约 8~11k，
    # 这里留 12k 给它 —— 估不准也只影响"早压一轮还是晚压一轮"，不影响完整性。
    try:
        await memory.compress_to_fit(
            db,
            conversation,
            budget_tokens=get_settings().prompt_budget_tokens - GROUP_PREFIX_RESERVE_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 —— 压缩是改善项，不能拖垮这一轮发言
        logger.warning("群聊压缩失败（本轮可能超预算，但不裁切）：%s", exc)

    active = summaries_repo.get_active(db, conversation.id)
    covered = active.covered_to_seq if active is not None else 0
    history_rows = messages_repo.list_before_seq(
        db, conversation.id, player_seq, get_settings().history_fetch_limit
    )
    history_rows = [row for row in history_rows if row.seq > covered]
    pairs = [
        (row.speaker or ("玩家" if row.side == "user" else ""), row.text) for row in history_rows
    ]
    transcript = build_transcript(pairs)
    last_speaker = _last_ai_speaker(history_rows)

    chosen = await _decide_speaker(
        db,
        conversation,
        members,
        speaker_slug=speaker_slug,
        mentions=mentions,
        mentioned=_mentioned_member(player_text, members),
        spoken=_spoken_this_round(history_rows),
        transcript=transcript,
        last_speaker=last_speaker,
    )

    if chosen is None:
        return None

    slug, name = chosen
    character = next(
        (db.get(Character, cid) for cid, s, _n, _o in members if s == slug), None
    )
    character_prompt = (
        content_repo.get_character_prompt(db, character.id, device_id)
        if character is not None
        else ""
    )

    messages = build_group_messages(
        safety_protocol=content_repo.get_safety_protocol(db),
        worldview=content_repo.get_text_config(db, "worldview"),
        speaker=name,
        character_prompt=character_prompt,
        members=[n for _c, _s, n, _o in members],
        player_role=conversation.player_role,
        group_name=conversation.title,
        transcript=transcript,
        bond=load_bond(db, conversation, character),
        summary=conversation.summary_head or "",
        memory=conversation.memory or "",
        player_message=player_text,
        local_time=local_time,
        time_enabled=time_enabled,
        immersive=immersive,
    )

    if not player_text.strip():
        # 「下一句」没有玩家新发言：补一句 OOC 指令，否则模型不知道自己该接话
        messages.append(
            {
                "role": "user",
                "content": (
                    f"【系统指令】接着上面的群聊往下接一句，你现在是「{name}」。"
                    "只写你这一句，不要替别人说话。"
                ),
            }
        )

    generation = await _inline(db, conversation, messages, immersive=immersive, think=think)
    return name, generation


async def _small_call(db: Session, system: str, user: str, *, max_tokens: int) -> str:
    """主持人这种小调用：冷启动用一把 key，不做会话粘性。"""
    pool = keys.get_pool(db)
    state = await keys.acquire_waiting(pool, None, get_settings().key_wait_seconds)
    if state is None:
        raise upstream.UpstreamError("现在上游忙不过来，稍等再试", status=429)
    try:
        reply = await upstream.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            credential=state.credential,
            think=False,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        pool.on_success(db, state)
        return reply.text.strip()
    except upstream.UpstreamError as exc:
        pool.on_failure(db, state, status=exc.status, network=exc.network)
        raise
    finally:
        pool.release(state)


def _last_ai_speaker(rows: list[Message]) -> str:
    for row in reversed(rows):
        if row.side == "assistant" and row.speaker:
            return row.speaker
    return ""


__all__ = ["pick_speaker", "reply_once"]
