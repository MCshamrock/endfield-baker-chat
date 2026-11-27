"""generate.py —— 出话：从库里的历史，到一条能存回库的回复。

这是"从零写好的后端"里第一块真正跟模型打交道的东西。整条链路：

    读世界观 + 读角色提示词 + 读历史
        → 拼 messages（prompt/dialog.py）
        → 调上游（upstream.py）或转老网关（gen_mode=gateway）
        → 后处理（postprocess.py：表情翻译 + 沉浸式过滤）
        → 存进 messages + 记一行 usage_events

两个必须记住的设计点：

1. **历史从库里读，不从请求里读。** 客户端只发"这一句"，历史是服务端的事。
   这正是 C/S 翻转的意义：客户端不再当数据库。
2. **回复和用户消息在同一个会话里顺序分配 seq。** 生成期间会话行是锁着的吗？
   不是 —— 见 `app/api/conversations.py` 的说明：先提交用户消息并释放锁，
   再生成，最后单独事务写回复。这样生成那几十秒不会把整个会话锁死，
   代价是"极端并发下回复可能比别人的晚一条"，可以接受（同一个人不会同时用两台设备狂发）。
"""

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import keys, memory, upstream
from app.clock import utcnow
from app.db import SessionLocal
from app.models import Character, Conversation, Message
from app.owners import owner_key
from app.postprocess import finalize_reply, translate_emoji_tokens
from app.prompt.bond import BOND_MAX_ROWS, format_bond
from app.prompt.budget import estimate_tokens, history_budget
from app.prompt.dialog import build_dialog_messages, compose_character_prompt, wrap_context
from app.prompt.rules import CONTINUE_RULE
from app.repos import content as content_repo
from app.repos import memories as memories_repo
from app.repos import messages as messages_repo
from app.repos import summaries as summaries_repo
from app.repos import usage as usage_repo
from app.repos import worldbook as worldbook_repo
from app.retrieval.store import build_index_text, prefetch
from app.settings import get_settings
from app.upstream import UpstreamError

logger = logging.getLogger("baker.generate")


@dataclass(slots=True)
class Generation:
    """一次生成的结果。字段和 usage_events 的列一一对应，方便直接记账。"""

    text: str
    # 上游思考过程。**只落库、不下发**（项目书 docs/08 §1.3：不对用户开放）
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    first_token_ms: int = 0
    total_ms: int = 0
    # 这次实际用的是哪把 key（粘性有没有生效，全靠这一列核对）
    api_key_id: int | None = None


@dataclass(slots=True)
class Prepared:
    """这一轮发给模型的全部材料（前缀 + 历史），**先算预算再决定压多少**。

    之所以把"前缀"和"历史"拆出来而不是一步拼完：
    压缩需要知道这一轮到底能给历史留多少额度，而额度 = 总预算 − 前缀 − 回复余量。
    不知道前缀是多少，就只能拍一个保守值，压多了白花钱、压少了会顶爆。
    """

    safety_protocol: str
    worldview: str
    worldbook_index: str
    worldbook_constants: str
    character_prompt: str
    character_content: str
    bond: str
    summary: str
    memory: str
    context: str
    history: list[dict[str, object]]

    def prefix_tokens(self) -> int:
        """前缀占多少 token（含检索正文那段包装）。"""
        return (
            estimate_tokens(self.safety_protocol)
            + estimate_tokens(self.worldview)
            + estimate_tokens(self.worldbook_index)
            + estimate_tokens(self.worldbook_constants)
            + estimate_tokens(self.character_content)
            + estimate_tokens(self.bond)
            + estimate_tokens(self.summary)
            + estimate_tokens(self.memory)
            + estimate_tokens(wrap_context(self.context) if self.context.strip() else "")
        )

    def history_budget(self) -> int:
        settings = get_settings()
        return history_budget(
            settings.prompt_budget_tokens, self.prefix_tokens(), settings.reply_reserve_tokens
        )


def load_bond(db: Session, conversation: Conversation, character: Character | None) -> str:
    """这一局要带的人情账。关掉读取就整块不出现（但写入照常，那是另一条路）。"""
    if character is None:
        return ""
    owner = owner_key(conversation.user_id, conversation.device_id)
    enabled = memories_repo.resolve_enabled(
        db, owner_key=owner, conversation_mode=conversation.memory_mode
    )
    if not enabled:
        return ""
    rows = memories_repo.list_active(
        db, owner_key=owner, character_id=character.id, limit=BOND_MAX_ROWS
    )
    if not rows:
        return ""
    memories_repo.touch_used(db, rows)
    return format_bond(rows)


def prepare(
    db: Session,
    conversation: Conversation,
    character: Character | None,
    user_text: str,
    *,
    immersive: bool,
    before_seq: int,
) -> Prepared:
    """把这一轮该用的材料从库里取出来（不含本轮输入）。"""
    settings = get_settings()
    safety_protocol = content_repo.get_safety_protocol(db)
    worldview = content_repo.get_text_config(db, "worldview")
    character_prompt = ""
    if character is not None:
        # 设备自己改过的人设优先（导入老数据包时带进来的），否则用全局版本
        character_prompt = content_repo.get_character_prompt(
            db, character.id, conversation.device_id
        )
    bond = load_bond(db, conversation, character)

    sum_row = summaries_repo.get_active(db, conversation.id)
    covered_to_seq = sum_row.covered_to_seq if sum_row is not None else 0
    summary_text = conversation.summary_head or ""
    memory_text = conversation.memory or ""

    rows = messages_repo.list_before_seq(
        db, conversation.id, before_seq, settings.history_fetch_limit
    )
    # 已经被摘要覆盖的那部分不再发原文（库里还在，只是不喂给模型）
    rows = [row for row in rows if row.seq > covered_to_seq]
    history: list[dict[str, object]] = [
        {"role": "assistant" if row.side == "assistant" else "user", "content": row.text}
        for row in rows
    ]

    # 世界书：索引（目录）每轮全量进 system；正文按关键词预取，受 token 预算约束。
    # 两者都来自同一份内存条目（repos/worldbook.py 缓存 60 秒）。
    worldbook_index = ""
    worldbook_constants = ""
    context = ""
    if settings.worldbook_enabled:
        entries = worldbook_repo.list_entries(db, settings.worldbook_slug)
        if entries:
            if settings.worldbook_index_enabled:
                worldbook_index, worldbook_constants = build_index_text(
                    entries, "明日方舟：终末地"
                )
            recent_user_texts = [
                str(item["content"]) for item in history if item["role"] == "user"
            ][-2:] + [user_text]
            context = prefetch(
                entries,
                recent_user_texts,
                budget_tokens=settings.worldbook_budget_tokens,
            )

    return Prepared(
        safety_protocol=safety_protocol,
        worldview=worldview,
        worldbook_index=worldbook_index,
        worldbook_constants=worldbook_constants,
        character_prompt=character_prompt,
        character_content=compose_character_prompt(character_prompt, immersive),
        bond=bond,
        summary=summary_text,
        memory=memory_text,
        context=context,
        history=history,
    )


def build_messages(
    db: Session,
    conversation: Conversation,
    character: Character | None,
    user_text: str,
    *,
    immersive: bool,
    before_seq: int,
    image_data_url: str = "",
    local_time: str = "",
    time_enabled: bool = True,
    prepared: Prepared | None = None,
) -> list[dict[str, object]]:
    """把"这一轮该发给模型什么"组装出来。单独一个函数，是为了能脱离网络单测。

    **这里不做任何裁切。** 历史该有多长由 `memory.compress_to_fit` 决定：
    超预算就把最老的一段压成摘要，而不是从列表里扔掉 —— 扔掉的永远找不回来。
    """
    item = prepared or prepare(
        db,
        conversation,
        character,
        user_text,
        immersive=immersive,
        before_seq=before_seq,
    )

    return build_dialog_messages(
        safety_protocol=item.safety_protocol,
        worldview=item.worldview,
        worldbook_index=item.worldbook_index,
        worldbook_constants=item.worldbook_constants,
        character_prompt=item.character_prompt,
        bond=item.bond,
        summary=item.summary,
        memory=item.memory,
        context=item.context,
        history=item.history,
        message=user_text,
        local_time=local_time,
        time_enabled=time_enabled,
        image_data_url=image_data_url,
        immersive=immersive,
    )


async def generate_reply(
    db: Session,
    *,
    conversation: Conversation,
    character: Character | None,
    user_message_seq: int,
    user_text: str,
    immersive: bool,
    think: bool,
    request_id: str,
    device_id: int,
    image_data_url: str = "",
    local_time: str = "",
    time_enabled: bool = True,
) -> Generation:
    """非流式出话。返回结果，**不写库正文**（调用方负责存回复）。

    这里会写 `usage_events`（同一个事务里），因为账必须和这次调用一起落地。
    """
    settings = get_settings()
    prepared = prepare(
        db,
        conversation,
        character,
        user_text,
        immersive=immersive,
        before_seq=user_message_seq,
    )

    # 超预算就压缩（压到装得下为止），**绝不裁切**。
    # 压缩本身失败时宁可这一轮超预算，也不丢任何一条消息。
    rounds = 0
    try:
        rounds = await memory.compress_to_fit(
            db, conversation, budget_tokens=prepared.history_budget()
        )
    except Exception as exc:  # noqa: BLE001 —— 压缩是改善项，绝不能拖垮回复
        logger.warning("压缩失败（本轮可能超预算，但不裁切）：%s", exc)
    if rounds:
        # 压过之后摘要头和覆盖点都变了，材料要重新取一遍
        prepared = prepare(
            db,
            conversation,
            character,
            user_text,
            immersive=immersive,
            before_seq=user_message_seq,
        )

    messages = build_messages(
        db,
        conversation,
        character,
        user_text,
        immersive=immersive,
        before_seq=user_message_seq,
        image_data_url=image_data_url,
        local_time=local_time,
        time_enabled=time_enabled,
        prepared=prepared,
    )

    started = time.monotonic()
    if settings.gen_mode == "gateway":
        reply = await _via_gateway(
            character=character,
            user_text=user_text,
            history=messages,
            immersive=immersive,
            think=think,
        )
        generation = reply
    else:
        generation = await _inline(db, conversation, messages, immersive=immersive, think=think)

    generation.total_ms = int((time.monotonic() - started) * 1000)
    if not generation.prompt_tokens:
        # 上游没给 usage（或走了网关）：按字符数粗估，至少让账面上不是 0。
        # 中文大约 1.5 字符 = 1 token，这个系数偏保守（宁多算不少算）。
        generation.prompt_tokens = _estimate_tokens("".join(str(m["content"]) for m in messages))
        generation.completion_tokens = _estimate_tokens(generation.text)

    usage_repo.record(
        db,
        request_id=request_id,
        device_id=device_id,
        conversation_id=conversation.id,
        model=settings.upstream_model,
        prompt_tokens=generation.prompt_tokens,
        completion_tokens=generation.completion_tokens,
        cached_tokens=generation.cached_tokens,
        first_token_ms=generation.first_token_ms,
        total_ms=generation.total_ms,
        api_key_id=generation.api_key_id,
    )
    logger.info(
        "生成完成 conv=%s mode=%s tokens=%s/%s %sms",
        conversation.public_id,
        settings.gen_mode,
        generation.prompt_tokens,
        generation.completion_tokens,
        generation.total_ms,
    )
    return generation


async def stream_text(
    messages: list[dict[str, object]], *, immersive: bool, think: bool
) -> AsyncIterator[str]:
    """把已拼好的 messages 流式送上游，逐段吐出**已后处理**的文本。

    为什么参数是 messages 而不是 db：
    流式响应的生命周期比请求长（几十秒），这个期间不该再持有数据库会话 ——
    会话要在开始流之前就归还。所以调用方先把 messages 拼好，这里只负责网络和后处理。

    后处理是流式安全的（`ImmersiveFilter` 跨增量记状态），客户端拿到的每一段都能直接显示。

    注意：流式拿不到上游的 usage（一般只在最后一帧给），这条路径**不记账**。
    记一个错的数比不记更糟 —— 第 4 刀做 key 亲和时改成让上游带
    `stream_options.include_usage`，那时才有真实数字。
    """
    from app.postprocess import ImmersiveFilter

    settings = get_settings()
    if settings.gen_mode == "gateway":
        raise UpstreamModeError(
            "gen_mode=gateway 时没有真正的流式：老网关这版只有整段返回，"
            "请改用非流式接口，或把 BAKER_GEN_MODE 切回 inline"
        )

    # 借一个 key：只在这个短会话里查库，流开始后不再持有数据库连接
    with SessionLocal() as db:
        pool = keys.get_pool(db)
        state = await keys.acquire_waiting(pool, None, settings.key_wait_seconds)
    if state is None:
        raise UpstreamError(
            "现在上游忙不过来（所有 key 都在冷却或占满），稍等再试", status=429
        )

    credential = state.credential
    failure: UpstreamError | None = None
    try:
        filter_state = ImmersiveFilter(enabled=immersive)
        async for delta in upstream.stream(messages, credential=credential, think=think):
            visible = filter_state.feed(translate_emoji_tokens(delta))
            if visible:
                yield visible
    except UpstreamError as exc:
        failure = exc
        raise
    finally:
        # 结算：成功就清零失败计数，失败按类型惩罚。用一个新会话，短命、不拖住流
        with SessionLocal() as db2:
            pool2 = keys.get_pool(db2)
            pool2.release(state)
            if failure is None:
                pool2.on_success(db2, state)
            else:
                pool2.on_failure(db2, state, status=failure.status, network=failure.network)
            db2.commit()


class UpstreamModeError(RuntimeError):
    """当前模式不支持这个能力（例如网关模式没有流式）。"""


async def _via_gateway(
    *,
    character: Character | None,
    user_text: str,
    history: list[dict[str, object]],
    immersive: bool,
    think: bool,
) -> Generation:
    """`gen_mode=gateway` 时转给老后端 `/chat`。

    老网关自己会拼提示词、做后处理，所以这里**不再二次处理**回复，
    否则表情会被翻译两次、括号会被过滤两次。

    这条路只为了过渡和对拍：老网关一旦下线，这个模式就作废。
    """
    import httpx

    settings = get_settings()
    url = f"{settings.gateway_url.rstrip('/')}/chat"
    # 老契约要的是"历史 + message"，不是我们拼好的 messages
    payload = {
        "character_id": character.slug if character else "",
        "message": user_text,
        "history": [m for m in history if m.get("role") != "system"],
        "use_rag": True,
        "think": think,
        "immersive_mode": immersive,
    }
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        response = await client.post(url, json=payload)
    response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    return Generation(
        text=str(data.get("reply") or ""),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
    )


async def _inline(
    db: Session,
    conversation: Conversation,
    messages: list[dict[str, object]],
    *,
    immersive: bool,
    think: bool,
) -> Generation:
    """内联路径：从池子里借一个 key → 调上游 → 结算（成功/惩罚）→ 回写粘性绑定。"""
    pool = keys.get_pool(db)
    state = await keys.acquire_waiting(
        pool, conversation.api_key_id, get_settings().key_wait_seconds
    )
    if state is None:
        raise UpstreamError(
            "现在上游忙不过来（所有 key 都在冷却或占满），稍等再试", status=429
        )

    credential = state.credential
    try:
        result = await upstream.complete(messages, credential=credential, think=think)
    except UpstreamError as exc:
        pool.on_failure(db, state, status=exc.status, network=exc.network)
        db.commit()  # 惩罚（冷却/禁用）必须落库，否则重启就忘了
        raise
    else:
        pool.on_success(db, state)
        # 粘性：这一轮实际用的就是这个 key，把绑定写回去（下一轮还打它，吃前缀缓存）
        if conversation.api_key_id != credential.key_id:
            conversation.api_key_id = credential.key_id
            db.flush()
        return Generation(
            text=finalize_reply(result.text, immersive=immersive),
            reasoning=result.reasoning,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            api_key_id=credential.key_id,
        )
    finally:
        pool.release(state)


async def one_shot(
    db: Session, messages: list[dict[str, object]], *, immersive: bool, think: bool
) -> "Generation":
    """不进库的一次生成（给老客户端兼容层用）。

    与 `generate_reply` 的区别：不读会话、不写消息、不记用量 ——
    老契约是无状态的，硬给它建会话只会造出一堆没人认领的空行。
    """
    pool = keys.get_pool(db)
    state = await keys.acquire_waiting(pool, None, get_settings().key_wait_seconds)
    if state is None:
        raise UpstreamError("现在上游忙不过来（所有 key 都在冷却或占满），稍等再试", status=429)

    started = time.monotonic()
    try:
        result = await upstream.complete(messages, credential=state.credential, think=think)
    except UpstreamError as exc:
        pool.on_failure(db, state, status=exc.status, network=exc.network)
        db.commit()
        raise
    else:
        pool.on_success(db, state)
        return Generation(
            text=finalize_reply(result.text, immersive=immersive),
            reasoning=result.reasoning,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens,
            total_ms=int((time.monotonic() - started) * 1000),
            api_key_id=state.credential.key_id,
        )
    finally:
        pool.release(state)


def _estimate_tokens(text: str) -> int:
    """粗估 token：中文大约 1.5 字符一个 token。只在拿不到真实 usage 时兜底。"""
    return int(len(text) / 1.5) if text else 0


def store_reply(
    db: Session,
    conversation: Conversation,
    generation: Generation,
    *,
    speaker: str = "",
    client_msg_id: str = "",
) -> Message:
    """把回复存成 assistant 消息（client_msg_id 默认 `a:{seq}`，绝不能留空）。

    `speaker` 只有群聊会用（这一句是谁说的）；单聊留空。
    `client_msg_id` 只有「继续说」会传 —— 它需要一个**可重放的幂等键**，
    否则客户端网络重试会在同一段话后面再存一条完全一样的。
    """
    return messages_repo.insert(
        db,
        conversation=conversation,
        client_msg_id=client_msg_id or f"a:{conversation.next_seq + 1}",
        side="assistant",
        text=generation.text,
        speaker=speaker,
        tokens=generation.completion_tokens,
        # 思考过程跟着这一句一起落库，但**永远不会被读出来发给客户端**
        reasoning=generation.reasoning,
    )


async def generate_continuation(
    db: Session,
    *,
    conversation: Conversation,
    character: Character | None,
    after_seq: int,
    local_time: str = "",
    time_enabled: bool = True,
    think: bool = False,
    request_id: str = "",
    device_id: int = 0,
) -> Generation:
    """「继续说」：不接玩家新消息，直接接着上一条 assistant 往下写。

    和 `generate_reply` 的区别只有一处：末尾多一条 `CONTINUE_RULE` 的用户消息。
    其余（压缩、预算、key 池、记用量）完全一样 —— 少一样都算两套逻辑。

    为什么必须显式给指令：不给的话模型看到"最后一轮是自己在说话"，
    要么把上一段重说一遍，要么开始总结、道别、换话题。这三种都是线上最常见的翻车。
    """
    settings = get_settings()
    prepared = prepare(db, conversation, character, "", immersive=True, before_seq=after_seq + 1)

    rounds = 0
    try:
        rounds = await memory.compress_to_fit(
            db, conversation, budget_tokens=prepared.history_budget()
        )
    except Exception as exc:  # noqa: BLE001 —— 压缩是改善项，不能拖垮这一轮
        logger.warning("压缩失败（继续说这一轮可能超预算，但不裁切）：%s", exc)
    if rounds:
        prepared = prepare(
            db, conversation, character, "", immersive=True, before_seq=after_seq + 1
        )

    messages = build_messages(
        db,
        conversation,
        character,
        "",
        immersive=True,
        before_seq=after_seq + 1,
        local_time=local_time,
        time_enabled=time_enabled,
        prepared=prepared,
    )
    messages.append({"role": "user", "content": CONTINUE_RULE})

    started = time.monotonic()
    generation = await _inline(db, conversation, messages, immersive=True, think=think)
    generation.total_ms = int((time.monotonic() - started) * 1000)
    if not generation.prompt_tokens:
        generation.prompt_tokens = _estimate_tokens(
            "".join(str(item["content"]) for item in messages)
        )
        generation.completion_tokens = _estimate_tokens(generation.text)

    usage_repo.record(
        db,
        request_id=request_id,
        device_id=device_id,
        conversation_id=conversation.id,
        model=settings.upstream_model,
        prompt_tokens=generation.prompt_tokens,
        completion_tokens=generation.completion_tokens,
        cached_tokens=generation.cached_tokens,
        first_token_ms=generation.first_token_ms,
        total_ms=generation.total_ms,
        api_key_id=generation.api_key_id,
    )
    logger.info(
        "继续说完成 conv=%s tokens=%s/%s %sms",
        conversation.public_id,
        generation.prompt_tokens,
        generation.completion_tokens,
        generation.total_ms,
    )
    return generation


def touch_conversation(conversation: Conversation) -> None:
    """回复写完顺手刷一下活跃时间（会话列表按它排序）。"""
    conversation.last_active_at = utcnow()


__all__ = [
    "Generation",
    "UpstreamError",
    "UpstreamModeError",
    "build_messages",
    "generate_continuation",
    "generate_reply",
    "load_bond",
    "prepare",
    "store_reply",
    "stream_text",
    "touch_conversation",
]
