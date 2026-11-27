"""memory.py —— 摘要与压缩：超预算就压，**绝不裁切**。

为什么压缩：上下文越长越慢（实测 15 万 token 开始降速），而且塞得越满越容易糊。
压缩的做法不是删消息，是**把要被挤出去的那段，压成一段前情提要**：

    原文一条都不删（库里还在），只是不再发给模型。

三条边界（都是从别人踩过的坑里学的）：

1. **保留最近 N 轮原文。** 刚刚说的话必须逐字在，摘要再准也会丢语气。
2. **摘要是滚动的。** 每次把「旧提要 + 新一段」重写成一份完整提要。
   只写新的一段、把旧的丢掉，等于每压一次就忘一段——那是慢慢失忆。
3. **压缩失败宁可超预算，也不裁切。** 少一点上下文只是慢，丢掉的话永远找不回来。
"""

import logging

from sqlalchemy.orm import Session

from app import keys, upstream
from app.models import Conversation, Message
from app.owners import owner_key
from app.prompt.budget import estimate_tokens
from app.repos import memories as memories_repo
from app.repos import messages as messages_repo
from app.repos import summaries as summaries_repo
from app.settings import get_settings

logger = logging.getLogger("baker.memory")

SUMMARY_SYSTEM = """你在帮一部角色扮演对话写"前情提要"，读者是接着往下演的模型。
要求：
1. 按剧情段落分条，每条一个 ≤14 字的短标题。
2. 只写：发生了什么、关系有什么变化、谁承诺过什么、埋了什么伏笔、留下了什么悬念。
3. 保留专有名词（人名、地名、物品名）原样，不要改写。
4. 不评价、不抒情、不复述完整台词。
5. 总长 800~1500 字。只输出提要本身，不要任何前言后语。"""

BOND_SYSTEM = """你在替一个角色维护"她记得玩家这个人"的账本。

给你两样东西：**已有的记忆条目**和**最新的一段剧情提要**。
你的任务：从新提要里挑出**关于玩家本人**的新事实，追加到账本上。

只记这 8 类（写英文 kind）：
  nickname   玩家叫什么（真名/网名）
  call       玩家希望她怎么称呼、或她自己的叫法
  appearance 玩家的外形特征（玩家自己说的才算）
  relation   玩家和她的关系状态（搭档/主顾/朋友…）
  preference 玩家喜欢什么
  dislike    玩家讨厌什么
  promise    玩家答应过什么、或她答应过玩家什么
  fact       玩家的身份/背景事实

规则：
1. 每行一条，格式严格是 `kind|内容`，内容不超过 40 字、只写一件事。
2. 已经在「已有记忆」里的不要再写（换个说法也不行）。
3. 不记：剧情过程、对第三人的评价、一时气话、联系方式与住址。
4. 最多 6 行。没有新的就只输出「无」。不要任何解释。"""

MEMORY_SYSTEM = """你在维护一部角色扮演对话的"永久记忆"。

给你两样东西：**已有的永久记忆条目**（只能追加，绝不许改写或删除）和**最新前情提要**。
你的任务只有一件：从新提要里提炼出**尚未出现在已有条目里的新事实**，追加到后面。

规则：
1. 只输出**新增**的条目，每条一行，以「· 」开头，每条不超过 40 字。
2. 已经有的事实不要重复、不要换说法重写、不要合并已有条目。
3. 只记：关系状态、重大事件（改变了什么）、明确的承诺与约定、重要设定物品、未解决的悬念。
4. 不记：日常闲聊、场景描写、已收尾的小事、重复内容。
5. 最多输出 3 条；如果新提要里没有值得记住的新事实，只输出「无」。
6. 不要输出解释、不要复述提要、不要任何前言后语。"""


async def compress_to_fit(db: Session, conversation: Conversation, *, budget_tokens: int) -> int:
    """把未覆盖的历史压到预算以内。返回压了几轮。

    调用时机：每轮生成之前（单聊、群聊都走这里）。

    **这个函数里没有任何"丢弃"分支。** 每轮做的都是同一件事：
    把最老的一段原文交给模型，变成一段更短的前情提要。原文还在库里，只是不再发。

    兜底仍然是"不裁切"：摘要调用失败、或者撞到轮数上限，就直接返回，
    这一轮可能超预算 —— 超预算只是慢，丢掉的话永远找不回来。
    """
    settings = get_settings()
    target = int(budget_tokens * settings.compress_to_ratio)
    rounds = 0

    while rounds < settings.compress_max_rounds:
        pending = _pending_rows(db, conversation)
        used = sum(estimate_tokens(row.text) + 4 for row in pending)
        if used <= target:
            return rounds

        chunk = _take_chunk(pending, settings.compress_chunk_tokens, settings.keep_recent_turns)
        if not chunk:
            # 只剩最近几轮、却还是超预算：说明最近这几条本身就是巨无霸。
            # 这时候压它们总比超预算好——内容进摘要，不是删掉。
            logger.warning(
                "最近 %s 条已超预算，退而压缩最近的一条 conv=%s", len(pending), conversation.public_id
            )
            chunk = pending[:1]

        if not await _compress_chunk(db, conversation, chunk):
            logger.error("压缩失败 conv=%s：本轮超预算，但不裁切任何内容", conversation.public_id)
            return rounds
        rounds += 1

    logger.error(
        "压缩轮数撞上限（%s）conv=%s：本轮可能超预算，但没有裁切",
        settings.compress_max_rounds,
        conversation.public_id,
    )
    return rounds


def _pending_rows(db: Session, conversation: Conversation) -> list[Message]:
    """还没进摘要的历史（从最早到最新）。"""
    active = summaries_repo.get_active(db, conversation.id)
    covered = active.covered_to_seq if active is not None else 0
    limit = get_settings().history_fetch_limit
    rows = messages_repo.list_before_seq(db, conversation.id, conversation.next_seq + 1, limit)
    return [row for row in rows if row.seq > covered]


def _take_chunk(rows: list[Message], cap_tokens: int, keep_turns: int) -> list[Message]:
    """从最老一端取一段（不含最近 `keep_turns` 轮）。

    至少取一条：单条消息本身就超过 cap 的时候也要取，否则它永远压不掉。
    """
    keep = max(1, keep_turns) * 2
    limit = max(0, len(rows) - keep)
    if limit == 0:
        return []

    taken: list[Message] = []
    used = 0
    for row in rows[:limit]:
        cost = estimate_tokens(row.text) + 4
        if taken and used + cost > cap_tokens:
            break
        taken.append(row)
        used += cost
    return taken


async def _compress_chunk(
    db: Session, conversation: Conversation, chunk: list[Message]
) -> bool:
    """把一段原文压进滚动摘要。返回是否成功（失败不抛，让调用方决定怎么办）。"""
    previous = (conversation.summary_head or "").strip()
    transcript = _format_transcript(chunk)

    try:
        summary = await _call_summary(db, transcript, previous=previous)
    except upstream.UpstreamError as exc:
        logger.warning("摘要失败（不影响本轮回复）：%s", exc)
        return False
    if len(summary) < 20:
        logger.warning("摘要太短，丢弃这次压缩（%s 字）", len(summary))
        return False

    summaries_repo.create(
        db,
        conversation=conversation,
        summary_text=summary,
        covered_to_seq=chunk[-1].seq,
    )
    # 二次压缩：从新提要里提炼"新增"条目，**追加**到永久记忆（不改写已有条目）。
    # 失败只保留旧记忆，不影响这轮回复。
    try:
        await _append_memory(db, conversation, summary)
    except upstream.UpstreamError as exc:
        logger.warning("永久记忆追加失败（保留旧记忆）：%s", exc)

    # 再往"她记得你这个人"的账本上记一笔（跨会话）。群聊不写，理由见 docs/04 §5.4。
    try:
        await _extract_bond(db, conversation, summary)
    except upstream.UpstreamError as exc:
        logger.warning("人情账提炼失败（保留旧账本）：%s", exc)

    db.commit()
    logger.info(
        "已压缩 conv=%s：%s 条 → 摘要 %s 字，覆盖到 seq=%s",
        conversation.public_id,
        len(chunk),
        len(summary),
        chunk[-1].seq,
    )
    return True


def _format_transcript(rows: list[Message]) -> str:
    """把消息排成"谁说了什么"。群聊用发言人名字，单聊用用户/角色。"""
    lines: list[str] = []
    for row in rows:
        who = "玩家" if row.side == "user" else (row.speaker or "角色")
        lines.append(f"{who}：{row.text}")
    return "\n".join(lines)


async def _append_memory(db: Session, conversation: Conversation, new_summary: str) -> None:
    """永久记忆：**只追加，不改写**。

    为什么不能像摘要那样整段重写：
      摘要的职责是"概括最近一段"，每次换掉是对的；
      永久记忆的职责是"一直记着"，一旦允许重写，模型每次都会顺手删几条，
      聊到后面"她答应过什么"就没了 —— 那是不可逆的信息丢失。
    所以这里只让模型输出**新增条目**，已有条目原样保留在库里。

    上限的取舍：这里**不做硬截断**（截断就等于删）。只在超过提醒线时打警告，
    让"记忆太长"这件事可见 —— 真要收，应该由人决定哪几条归档，而不是让模型偷偷丢。
    """
    settings = get_settings()
    existing = (conversation.memory or "").strip()
    material = (
        f"【已有永久记忆条目】\n{existing}\n\n【最新前情提要】\n{new_summary}"
        if existing
        else f"【已有永久记忆条目】\n（空）\n\n【最新前情提要】\n{new_summary}"
    )

    reply = await _call_llm(db, MEMORY_SYSTEM, material, max_tokens=settings.memory_max_tokens)
    additions = _parse_memory_items(reply)
    if not additions:
        logger.info("这次没有新的可记忆事实 conv=%s", conversation.public_id)
        return

    conversation.memory = "\n".join([existing, *additions]) if existing else "\n".join(additions)
    db.flush()

    total_chars = len(conversation.memory)
    logger.info(
        "永久记忆 +%s 条，共 %s 条 / %s 字 conv=%s",
        len(additions),
        len(_parse_memory_items(conversation.memory)),
        total_chars,
        conversation.public_id,
    )
    if total_chars > settings.memory_warn_chars:
        logger.warning(
            "永久记忆已到 %s 字（提醒线 %s）：它每轮都进 system，该考虑人工归档了",
            total_chars,
            settings.memory_warn_chars,
        )


async def _extract_bond(db: Session, conversation: Conversation, summary: str) -> int:
    """从新提要里提炼玩家本人的事实，写进跨会话账本。返回写入条数。

    只在单聊里做：群聊里她说的话未必是对玩家说的，写进去会污染单聊账本。
    """
    if conversation.character_id is None:
        return 0
    owner = owner_key(conversation.user_id, conversation.device_id)
    existing = memories_repo.list_active(
        db, owner_key=owner, character_id=conversation.character_id
    )
    known = "\n".join(f"· [{row.kind}] {row.text}" for row in existing) or "（空）"
    material = f"【已有记忆】\n{known}\n\n【最新提要】\n{summary}"

    reply = await _call_llm(db, BOND_SYSTEM, material, max_tokens=600)
    count = 0
    for line in reply.splitlines():
        body = line.strip().lstrip("·-•").strip()
        if "|" not in body:
            continue
        kind, text = body.split("|", 1)
        row = memories_repo.add(
            db,
            owner_key=owner,
            character_id=conversation.character_id,
            kind=kind.strip().lower(),
            text=text.strip(),
            source_conversation_id=conversation.id,
        )
        if row is not None:
            count += 1
    if count:
        logger.info("人情账 +%s 条 conv=%s", count, conversation.public_id)
    return count


def _parse_memory_items(text: str) -> list[str]:
    """把模型输出解析成条目列表。

    只认以「·」「-」「•」开头的行；返回「无」/空/解释性文字时返回空列表
    —— 宁可这轮不加，也不要往记忆里塞一句废话。
    """
    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] in "·-•":
            item = line[1:].strip()
            if item and item not in ("无", "（无）"):
                items.append(f"· {item}")
    return items[:3]


async def _call_summary(db: Session, transcript: str, *, previous: str = "") -> str:
    """调上游写摘要（滚动）：旧提要 + 新一段 → 一份新的完整提要。

    **不截断输入。** 切给压缩器的段本来就受 `compress_chunk_tokens` 约束；
    真遇到单条巨无霸，一次大一点的摘要调用也比把它砍掉强。
    """
    settings = get_settings()
    if previous:
        material = (
            f"【已有的前情提要】\n{previous}\n\n"
            f"【这一段新剧情（按时间顺序）】\n{transcript}\n\n"
            "把两者合并重写成**一份完整的前情提要**：已有提要里仍然重要的信息必须保留，"
            "新剧情按同样的粒度并进去，不要分成两份、不要写「以上是旧提要」这类话。"
        )
    else:
        material = transcript
    return await _call_llm(db, SUMMARY_SYSTEM, material, max_tokens=settings.summary_max_tokens)


async def _call_llm(db: Session, system: str, user: str, *, max_tokens: int) -> str:
    """内部小调用：摘要/记忆共用。单独的凭证、低温、短输出。"""
    pool = keys.get_pool(db)
    state = await keys.acquire_waiting(pool, None, get_settings().key_wait_seconds)
    if state is None:
        raise upstream.UpstreamError("现在上游忙不过来，稍等再试", status=429)
    try:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        reply = await upstream.complete(
            messages,
            credential=state.credential,
            think=False,
            max_tokens=max_tokens,
            temperature=0.2,
        )
        pool.on_success(db, state)
        return reply.text.strip()
    except upstream.UpstreamError as exc:
        pool.on_failure(db, state, status=exc.status, network=exc.network)
        raise
    finally:
        pool.release(state)


__all__ = ["compress_to_fit"]
