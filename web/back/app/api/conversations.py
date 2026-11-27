"""api/conversations.py —— 会话与消息（含出话）。

这一组端点是"架构翻过来了"的证据：客户端只发「会话指针 + 新事件 + 幂等键」，
历史、顺序、去重、提示词、调模型全在服务端。

三个必须看懂的地方：

1. **写消息分两段事务。** 先锁行分配 seq、提交用户消息；再生成；最后重新锁一次行、
   写回复、再提交。为什么不一口气做完：生成要几秒到几十秒，期间一直握着会话行锁，
   同一会话的另一条请求会死等。分成两段后，锁只在毫秒级持有。

2. **生成失败不影响用户消息。** 用户消息已经提交（弱网重发不会丢），
   接口回 502，客户端可以用**同一个 client_msg_id** 重发 —— 会拿到
   `duplicate:true` 和 `reply:null`，说明"话存住了、回复没出来"。

3. **流式端点的数据库连接在开始流之前就还掉。** 流可能持续几十秒，
   这段时间不能再占着一个连接；所以回复在流结束时用**新的会话**落库。
   也正因为这样：流式那条路**不记账**（拿不到 usage，记错的数比不记更糟）。
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app import errors, generate, group, ratelimit, suggestions
from app.clock import local_time_text, to_iso_str, utcnow
from app.db import SessionLocal
from app.deps import CurrentIdentity, DbSession, Identity
from app.models import Character, Conversation, Device, Message
from app.repos import attachments as attachments_repo
from app.repos import characters as characters_repo
from app.repos import conversations as conversations_repo
from app.repos import memories as memories_repo
from app.repos import messages as messages_repo
from app.schemas import (
    ContinueOut,
    ContinueRequest,
    ConversationCreateRequest,
    ConversationListOut,
    ConversationOut,
    MemberWeightOut,
    MessageCreatedOut,
    MessageCreateRequest,
    MessageListOut,
    MessageOut,
    NextTurnOut,
    SuggestionsOut,
    UsageOut,
)
from app.settings import get_settings

logger = logging.getLogger("baker.api")

router = APIRouter(tags=["conversations"])


# ---------------------------------------------------------------- 会话
@router.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(
    req: ConversationCreateRequest, db: DbSession, identity: CurrentIdentity
) -> ConversationOut:
    """开一个新会话。单聊传 `character_id`；群聊传 `mode=group` + `members`。"""
    if req.mode == "group":
        return _create_group(db, identity, req)

    if not req.character_id:
        raise errors.bad_request("单聊必须传 character_id（角色 slug）")

    character = characters_repo.find_by_slug(db, req.character_id)
    if character is None:
        raise errors.no_character()

    conversation = conversations_repo.create(
        db,
        device_id=identity.device.id,
        user_id=identity.user_id,
        character_id=character.id,
        mode=req.mode,
        title=req.title or character.name,
    )
    conversation.memory_mode = req.memory
    db.commit()
    return _to_out(conversation, character.slug)


def _create_group(
    db: DbSession, identity: Identity, req: ConversationCreateRequest
) -> ConversationOut:
    """建群聊：成员至少两个，重复的 slug 会被去掉（同一个人不进群两次）。"""
    slugs: list[str] = []
    for slug in req.members:
        if slug and slug not in slugs:
            slugs.append(slug)
    if len(slugs) < 2:
        raise errors.bad_request("群聊至少要有两个成员")

    characters = []
    for slug in slugs:
        character = characters_repo.find_by_slug(db, slug)
        if character is None:
            raise errors.AppError(
                "no_character", f"群成员里有不认识的角色：{slug}", http_status=404
            )
        characters.append(character)

    conversation = conversations_repo.create(
        db,
        device_id=identity.device.id,
        user_id=identity.user_id,
        character_id=None,
        mode="group",
        title=req.title or "、".join(item.name for item in characters),
    )
    conversation.speak_mode = req.speak_mode
    conversation.player_role = req.player_role
    conversation.memory_mode = req.memory
    conversations_repo.create_members(
        db, conversation_id=conversation.id, character_ids=[item.id for item in characters]
    )
    db.commit()
    # 建群响应也把成员权重带上：刚建完群弹「调权重」的界面，不用再多请求一次
    member_weights = _weights_of(
        db,
        identity.owner_key,
        [(item.id, item.slug, item.name, index) for index, item in enumerate(characters)],
    )
    return _to_out(
        conversation,
        None,
        [item.name for item in characters],
        member_weights,
    )


@router.get("/conversations", response_model=ConversationListOut)
def list_conversations(db: DbSession, identity: CurrentIdentity) -> ConversationListOut:
    """这台设备的会话列表，最近活跃的在前。

    群聊的成员一次性查出来（不是每条会话查一次）—— 客户端要靠它画头像叠层，
    缺了就只能拿标题猜"这群里有谁"。
    """
    rows = conversations_repo.list_for_owner(
        db, user_id=identity.user_id, device_id=identity.device.id
    )
    group_ids = [conversation.id for conversation, _slug in rows if conversation.mode == "group"]
    member_map = conversations_repo.members_by_conversation(db, group_ids)
    items = [
        _to_out(
            conversation,
            slug,
            member_map.get(conversation.id) if conversation.mode == "group" else None,
        )
        for conversation, slug in rows
    ]
    return ConversationListOut(items=items)


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str, db: DbSession, identity: CurrentIdentity
) -> ConversationOut:
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, slug = row
    if conversation.mode != "group":
        # 单聊没有"群成员权重"这回事：members 和 member_weights 都给空列表
        return _to_out(conversation, slug, [], [])

    member_rows = conversations_repo.list_members(db, conversation.id)
    members = [name for _cid, _slug, name, _ord in member_rows]
    # 顺序和 members 一一对应：前端可以 zip 着画"头像 + 权重"
    member_weights = _weights_of(db, identity.owner_key, member_rows)
    return _to_out(conversation, slug, members, member_weights)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str, db: DbSession, identity: CurrentIdentity
) -> None:
    """删会话（软删）。消息行还在库里，便于排查。"""
    conversation = conversations_repo.lock_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if conversation is None:
        raise errors.no_conversation()
    conversations_repo.soft_delete(db, conversation)
    db.commit()


# ---------------------------------------------------------------- 消息
async def _group_turn(
    db: DbSession,
    conversation: Conversation,
    req: MessageCreateRequest,
    stored: "_StoredMessage",
    identity: Identity,
    request: Request,
) -> MessageCreatedOut:
    """群聊的一轮：决定谁说话 → 生成一条 → 存库。

    主持人判定 `END`（这个话题聊完了）时**返回 silent=True 且 reply=null**，
    不硬凑一句 —— 群聊里最假的就是"没人想说话还要有人说话"。
    """
    members = conversations_repo.list_members(db, conversation.id)
    try:
        outcome = await group.reply_once(
            db,
            conversation=conversation,
            members=members,
            player_seq=stored.seq,
            player_text=req.text,
            speaker_slug=req.assign_speaker or None,
            mentions=req.mentions or None,
            local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
            time_enabled=req.time_enabled,
            immersive=req.immersive,
            think=req.think,
            device_id=identity.device.id,
            request_id=_request_id(request),
        )
    except generate.UpstreamError as exc:
        raise errors.AppError(
            "upstream_error",
            f"话已经存下了，但上游没回：{exc}",
            http_status=502,
            details={"seq": stored.seq},
        ) from exc

    if outcome is None:
        db.commit()  # 玩家这条消息照样要留下
        return MessageCreatedOut(
            seq=stored.seq,
            duplicate=False,
            reply=None,
            silent=True,
            created_at=to_iso_str(stored.created_at),
        )

    speaker_name, generation = outcome
    relocked = conversations_repo.lock_owned(
        db, conversation.public_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if relocked is None:
        raise errors.no_conversation()
    reply = generate.store_reply(db, relocked, generation, speaker=speaker_name)
    generate.touch_conversation(relocked)
    db.commit()

    return MessageCreatedOut(
        seq=stored.seq,
        duplicate=False,
        reply=reply.text,
        reply_seq=reply.seq,
        speaker=speaker_name,
        usage=UsageOut(
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            cached_tokens=generation.cached_tokens,
            total_ms=generation.total_ms,
        ),
        created_at=to_iso_str(stored.created_at),
    )


@router.post("/conversations/{conversation_id}/messages", response_model=MessageCreatedOut)
async def post_message(
    conversation_id: str,
    req: MessageCreateRequest,
    db: DbSession,
    identity: CurrentIdentity,
    request: Request,
) -> MessageCreatedOut:
    """写一条用户消息；默认接着让服务端出话。

    `duplicate=true` 表示这个 client_msg_id 之前存过，服务端没有新建、也不会再生成。

    `regenerate=true` 走另一条路（见 _regenerate_turn）：**不新增玩家消息**，
    只把这一轮的回话重写一遍。客户端"再发一遍同一句"会让库里多出一条一模一样的
    玩家消息 —— 模型看到的是玩家说了两遍。
    """
    if req.regenerate:
        return await _regenerate_turn(db, conversation_id, req, identity, request)

    # 老客户端兼容：4.0 之前的客户端点「重新生成」＝把玩家那句话**再发一遍**。
    # 服务端完全看不出这和"玩家又说了一遍"的区别，于是照常插入一条新的玩家消息、
    # 也照常接着上一句（那句"聊炸了"的回话还在历史里）往下编 —— 线上就是
    # 「重新生成几乎没有效果」＋「重生成几次后她说我为什么说好几遍」。
    # 这里做一条宽容规则（见 _plan_client_echo）：同一句、且在时间窗内 → 按重生成处理。
    echo_plan = _plan_client_echo(db, conversation_id, req, identity)
    if echo_plan is not None:
        return await _regenerate_turn(
            db, conversation_id, req, identity, request, plan=echo_plan
        )

    stored = _store_user_message(db, conversation_id, identity, req)
    if stored.duplicate:
        return _out_of(stored)

    conversation = stored.conversation
    if not req.generate:
        return _out_of(stored)

    # 只保留每人每分钟的额度；**不限制同时在线的用户数**
    # （借不到 key 时由 keys.acquire_waiting 等着，而不是把请求打回去）
    _check_rate(identity.device)
    if conversation.mode == "group":
        return await _group_turn(db, conversation, req, stored, identity, request)
    return await _single_turn(db, conversation, req, stored, identity, request)


async def _single_turn(
    db: DbSession,
    conversation: Conversation,
    req: MessageCreateRequest,
    stored: "_StoredMessage",
    identity: Identity,
    request: Request,
) -> MessageCreatedOut:
    """单聊一轮：生成 → 重新锁行 → 存回复。"""
    character = _character_of(db, conversation)
    image_data_url = _image_of(db, stored)
    try:
        generation = await generate.generate_reply(
            db,
            conversation=conversation,
            character=character,
            user_message_seq=stored.seq,
            user_text=req.text,
            immersive=req.immersive,
            think=req.think,
            request_id=_request_id(request),
            device_id=identity.device.id,
            image_data_url=image_data_url,
            local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
            time_enabled=req.time_enabled,
        )
    except generate.UpstreamError as exc:
        raise errors.AppError(
            "upstream_error",
            f"话已经存下了，但上游没回：{exc}",
            http_status=502,
            details={"seq": stored.seq},
        ) from exc

    # 重新锁一次行：生成期间可能有人也在写，next_seq 要以库里的为准
    relocked = conversations_repo.lock_owned(
        db, conversation.public_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if relocked is None:
        raise errors.no_conversation()
    reply = generate.store_reply(db, relocked, generation)
    generate.touch_conversation(relocked)
    db.commit()

    return MessageCreatedOut(
        seq=stored.seq,
        duplicate=False,
        reply=reply.text,
        reply_seq=reply.seq,
        usage=UsageOut(
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            cached_tokens=generation.cached_tokens,
            total_ms=generation.total_ms,
        ),
        created_at=to_iso_str(stored.created_at),
    )


@dataclass
class _RegenPlan:
    """「重新生成」要动哪一条，以及要不要清掉旧回复。

    mode 的三种取值见 _plan_regenerate。anchor 在 mode='missing' 时为 None。
    """

    mode: str
    anchor: Message | None = None


def _plan_client_echo(
    db: DbSession, conversation_id: str, req: MessageCreateRequest, identity: Identity
) -> _RegenPlan | None:
    """老客户端兼容：这次"发消息"其实是「重新生成」吗？

    4.0 之前的客户端点「重新生成」时，会把**玩家那句话再发一遍**（不带任何标记）。
    从服务端看，这跟"玩家又说了一遍同一句"完全一样 —— 于是会：
      1. 再插一条一模一样的玩家消息（历史里出现两遍 → 她说"你怎么说好几遍"）；
      2. 那句"聊炸了"的回话还在历史里，新回复当然还是接着它编
         （→ 「重新生成几乎没有效果」）。
    这两条正是 2026-11-22 群里反馈的现场。

    所以这里定一条宽容规则：**正文与最后一条玩家消息一字不差，而且那条是刚刚发的**
    → 按「重新生成」处理（清掉旧回复、就地重写，不新增玩家消息）。
    这条规则对老客户端有效，对新客户端也没有副作用（新客户端会直接带 `regenerate`）。

    几个刻意的边界：
      * 群聊不管：群聊那一轮的历史是客户端带上来的，走的是另一套。
      * 带新附件的不算：连发两张图时正文都是「[图片]」，按这句去匹配会吞掉第二张。
      * 时间窗（`regenerate_echo_seconds`，默认 5 分钟）：真·重复发言只会发生在
        读到回话之后马上重发；隔半天又发同一句的，仍然老老实实算两条新消息。
      * 锚点后面只允许是"已经生成好的回话"；出现别的玩家消息就说明对话往前走了，
        这时候不做任何猜测。
    """
    if not req.generate:
        return None
    if req.attachment_id:
        return None
    want = req.text.strip()
    if not want:
        return None

    window = get_settings().regenerate_echo_seconds
    if window <= 0:
        return None

    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        return None
    conversation, _slug = row
    if conversation.mode == "group":
        return None

    # 同一条 client_msg_id 的重放是**幂等重试**，不是重新生成：
    # 交给 _store_user_message 回 duplicate=true（顺手也省掉一次上游调用）。
    if messages_repo.find_by_client_msg_id(db, conversation.id, req.client_msg_id) is not None:
        return None

    anchor = messages_repo.last_user(db, conversation.id)
    if anchor is None or anchor.text.strip() != want:
        return None
    if anchor.created_at is None:
        return None
    if utcnow() - anchor.created_at > timedelta(seconds=window):
        return None

    tail = messages_repo.list_after_seq(db, conversation.id, anchor.seq, 20)
    if any(item.side != "assistant" for item in tail):
        return None

    logger.info(
        "重新生成（老客户端兼容）：conv=%s 正文与 seq=%s 相同、%s 秒内重发，按重生成处理",
        conversation.id,
        anchor.seq,
        int((utcnow() - anchor.created_at).total_seconds()),
    )
    return _RegenPlan("tail", anchor)


def _plan_regenerate(
    db: DbSession, conversation: Conversation, req: MessageCreateRequest
) -> _RegenPlan:
    """算出「重新生成」的锚点。

    客户端只发正文（本地没有服务端 seq），所以锚点只能靠正文找：

      * 正文等于**最后一条**玩家消息 → `tail`：先把这条之后的旧回复删掉，
        新回复因此落回原来的位置。这是绝大多数情况（重生成刚说完那轮的回话）。
      * 正文只匹配到**更早**的某一轮 → `older`：库一动不动，生成完只回给客户端。
        删掉后面所有轮次是真丢数据；而那一条又不该重复落库 —— 只能不存。
      * 库里根本没有这句 → `missing`：服务端还没收到过它（刚绑定会话、
        或者上一次压根没送到），按正常发消息处理。
    """
    anchor = messages_repo.last_user(db, conversation.id)
    if anchor is None:
        return _RegenPlan("missing")
    want = req.text.strip()
    if not want or anchor.text.strip() == want:
        return _RegenPlan("tail", anchor)
    older = messages_repo.find_last_user_by_text(db, conversation.id, req.text)
    if older is None:
        return _RegenPlan("missing")
    return _RegenPlan("older", older)


def _clear_tail_for_regenerate(
    db: DbSession, conversation: Conversation, anchor_seq: int, identity: Identity
) -> Conversation | None:
    """删掉锚点之后的旧回复，并把 next_seq 退回去。

    退回 next_seq 是"回复落回原位"的关键：新回复会重新拿到 anchor_seq + 1，
    于是 `/messages` 的排序仍然是"提问 → 回答 → 提问 → 回答"。
    """
    locked = conversations_repo.lock_owned(
        db,
        conversation.public_id,
        user_id=identity.user_id,
        device_id=identity.device.id,
    )
    if locked is None:
        return None
    removed = messages_repo.delete_after_seq(db, locked.id, anchor_seq)
    locked.next_seq = anchor_seq
    db.commit()
    if removed:
        logger.info(
            "重新生成：conv=%s 清掉 seq>%s 的 %s 条旧回复", locked.id, anchor_seq, removed
        )
    return locked


async def _regenerate_turn(
    db: DbSession,
    conversation_id: str,
    req: MessageCreateRequest,
    identity: Identity,
    request: Request,
    plan: "_RegenPlan | None" = None,
) -> MessageCreatedOut:
    """重新生成一轮：不新增玩家消息，只把这一轮的回话重写一遍。

    为什么必须由服务端做这个动作：历史在服务端。客户端"再发一遍同一句"会写进
    一条新的玩家消息，历史里于是出现**两句一模一样的话** —— 线上表现就是
    "我明明只叫了她一声，她说我叫了两次"。

    `plan` 非空时直接用（老客户端兼容那条路已经算好了锚点，见 _plan_client_echo）。
    """
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, _slug = row
    if conversation.mode == "group":
        raise errors.AppError(
            "regenerate_unsupported",
            "群聊的重新生成走群聊那一轮，不走单聊接口",
            http_status=400,
        )

    plan = plan or _plan_regenerate(db, conversation, req)
    if plan.mode == "missing":
        # 服务端还没有这句：按正常发消息处理（不重复，因为客户端这次没把它算进历史）
        stored = _store_user_message(db, conversation_id, identity, req)
        if stored.duplicate:
            return _out_of(stored)
        _check_rate(identity.device)
        return await _single_turn(db, stored.conversation, req, stored, identity, request)

    assert plan.anchor is not None
    anchor = plan.anchor
    # commit 之后 ORM 对象会过期，先把要用的值抓下来
    anchor_seq = anchor.seq
    anchor_text = anchor.text
    anchor_created_at = anchor.created_at
    anchor_seq_for_image = anchor.attachment_id

    if plan.mode == "tail":
        relocked = _clear_tail_for_regenerate(db, conversation, anchor_seq, identity)
        if relocked is None:
            raise errors.no_conversation()
        conversation = relocked

    _check_rate(identity.device)
    character = _character_of(db, conversation)
    try:
        generation = await generate.generate_reply(
            db,
            conversation=conversation,
            character=character,
            user_message_seq=anchor_seq,
            user_text=anchor_text,
            immersive=req.immersive,
            think=req.think,
            request_id=_request_id(request),
            device_id=identity.device.id,
            image_data_url=_image_of_attachment(db, anchor_seq_for_image),
            local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
            time_enabled=req.time_enabled,
        )
    except generate.UpstreamError as exc:
        raise errors.AppError(
            "upstream_error",
            f"重新生成失败，上游没回：{exc}",
            http_status=502,
            details={"seq": anchor_seq},
        ) from exc

    reply_seq: int | None = None
    if plan.mode == "tail":
        relocked = conversations_repo.lock_owned(
            db,
            conversation.public_id,
            user_id=identity.user_id,
            device_id=identity.device.id,
        )
        if relocked is None:
            raise errors.no_conversation()
        reply = generate.store_reply(db, relocked, generation)
        generate.touch_conversation(relocked)
        db.commit()
        reply_seq = reply.seq
    else:
        # 更早的一轮：只回给客户端，不落库（见 _plan_regenerate 的说明）
        generate.touch_conversation(conversation)
        db.commit()

    return MessageCreatedOut(
        seq=anchor_seq,
        duplicate=False,
        reply=generation.text,
        reply_seq=reply_seq,
        usage=UsageOut(
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            cached_tokens=generation.cached_tokens,
            total_ms=generation.total_ms,
        ),
        created_at=to_iso_str(anchor_created_at),
    )


@router.post("/conversations/{conversation_id}/continue", response_model=ContinueOut)
async def continue_message(
    conversation_id: str,
    req: ContinueRequest,
    db: DbSession,
    identity: CurrentIdentity,
    request: Request,
) -> ContinueOut:
    """「继续说」：不接新消息，让角色接着上一条往下写。

    为什么单独一个端点而不是"再发一条空消息"：空消息会进历史、会占 seq、
    还会让客户端在列表里多出一条空白气泡。这里只产生一条 assistant 消息。

    群聊暂不支持（多一个"选谁接"的分支，见 docs/07）。单聊没有歧义，先做透。
    """
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, slug = row
    if conversation.mode == "group":
        raise errors.AppError(
            "continue_unsupported",
            "群聊暂不支持「继续说」，直接发一句话让她接话即可",
            http_status=400,
        )

    idem = f"cont:{req.client_msg_id}"
    existing = messages_repo.find_by_client_msg_id(db, conversation.id, idem)
    if existing is not None:
        return ContinueOut(
            reply=existing.text,
            reply_seq=existing.seq,
            duplicate=True,
            created_at=to_iso_str(existing.created_at),
        )

    last = messages_repo.last_assistant(db, conversation.id)
    if last is None:
        raise errors.AppError(
            "nothing_to_continue", "还没有可以接着说的话，先说一句吧", http_status=400
        )

    _check_rate(identity.device)
    try:
        generation = await generate.generate_continuation(
            db,
            conversation=conversation,
            character=_character_of(db, conversation),
            after_seq=last.seq,
            local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
            time_enabled=req.time_enabled,
            think=req.think,
            request_id=_request_id(request),
            device_id=identity.device.id,
        )
    except generate.UpstreamError as exc:
        raise errors.AppError("upstream_error", f"上游没回：{exc}", http_status=502) from exc

    relocked = conversations_repo.lock_owned(
        db, conversation.public_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if relocked is None:
        raise errors.no_conversation()
    reply = generate.store_reply(db, relocked, generation, client_msg_id=idem)
    generate.touch_conversation(relocked)
    db.commit()

    return ContinueOut(
        reply=reply.text,
        reply_seq=reply.seq,
        usage=UsageOut(
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            cached_tokens=generation.cached_tokens,
            total_ms=generation.total_ms,
        ),
        created_at=to_iso_str(reply.created_at),
    )


@router.post("/conversations/{conversation_id}/suggestions", response_model=SuggestionsOut)
async def conversation_suggestions(
    conversation_id: str, db: DbSession, identity: CurrentIdentity
) -> SuggestionsOut:
    """给这个会话生成 3 条「我接下来可以说什么」。

    客户端应该在**玩家点开推荐面板时**才调，不要每轮都调 ——
    这功能每次都花一次上游调用，每轮拉会让限速提前撞满。
    """
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, _slug = row
    character = _character_of(db, conversation)

    items = await suggestions.suggest_for_conversation(
        db, conversation=conversation, character=character
    )
    return SuggestionsOut(suggestions=items)


@router.post("/conversations/{conversation_id}/next", response_model=NextTurnOut)
async def next_turn(
    conversation_id: str,
    req: ContinueRequest,
    db: DbSession,
    identity: CurrentIdentity,
    request: Request,
) -> NextTurnOut:
    """群聊「下一句」：不接玩家新消息，按发言策略再让一位说一句。

    这是"直到所有人发言完毕"那条链的发动机：客户端发完一句 → 收到一条 →
    等两秒 → 调这里要下一位 → 直到 `done=true`。

    为什么不让服务端自己连发：那会占住一个连接十几秒，用户点停止也停不下来，
    而且中间没有任何插话的缝隙 —— 那正是"假群聊"最明显的特征。
    """
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, _slug = row
    if conversation.mode != "group":
        raise errors.AppError(
            "not_group", "只有群聊有「下一句」；单聊用 /continue", http_status=400
        )

    idem = f"next:{req.client_msg_id}"
    existing = messages_repo.find_by_client_msg_id(db, conversation.id, idem)
    if existing is not None:
        return NextTurnOut(
            reply=existing.text,
            reply_seq=existing.seq,
            speaker=existing.speaker,
            done=False,
            duplicate=True,
            created_at=to_iso_str(existing.created_at),
        )

    members = conversations_repo.list_members(db, conversation.id)
    if not group.unspoken_names(db, conversation, members):
        return NextTurnOut(done=True, remaining=[], created_at=to_iso_str(utcnow()))

    _check_rate(identity.device)
    try:
        outcome = await group.reply_once(
            db,
            conversation=conversation,
            members=members,
            player_seq=conversation.next_seq + 1,
            player_text="",
            speaker_slug=req.speaker or None,
            local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
            time_enabled=req.time_enabled,
            immersive=True,
            think=req.think,
            device_id=identity.device.id,
            request_id=_request_id(request),
        )
    except generate.UpstreamError as exc:
        raise errors.AppError("upstream_error", f"上游没回：{exc}", http_status=502) from exc

    if outcome is None:
        db.commit()
        return NextTurnOut(done=True, remaining=[], created_at=to_iso_str(utcnow()))

    speaker, generation = outcome
    relocked = conversations_repo.lock_owned(
        db, conversation.public_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if relocked is None:
        raise errors.no_conversation()
    reply = generate.store_reply(
        db, relocked, generation, speaker=speaker, client_msg_id=idem
    )
    generate.touch_conversation(relocked)
    remaining = group.unspoken_names(db, relocked, members)
    db.commit()

    return NextTurnOut(
        reply=reply.text,
        reply_seq=reply.seq,
        speaker=speaker,
        done=not remaining,
        remaining=remaining,
        usage=UsageOut(
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            cached_tokens=generation.cached_tokens,
            total_ms=generation.total_ms,
        ),
        created_at=to_iso_str(reply.created_at),
    )


@router.post("/conversations/{conversation_id}/messages:stream")
async def stream_message(
    conversation_id: str,
    req: MessageCreateRequest,
    db: DbSession,
    identity: CurrentIdentity,
) -> StreamingResponse:
    """写一条用户消息，然后以 SSE 逐段吐回复。

    帧格式（就三种）：
        event: delta   data: {"text": "…"}          正文增量，客户端直接拼
        event: done    data: {"seq": 12, "reply_seq": 13}
        event: error   data: {"code": "upstream_error", "message": "…"}

    先落用户消息再开始流：这样即使流断了，用户说的话也不会丢。
    """
    settings = get_settings()
    if settings.gen_mode == "gateway":
        raise errors.AppError(
            "no_stream_in_gateway_mode",
            "当前是 gateway 模式（转老网关），它这版只有整段返回，请改用非流式接口",
            http_status=400,
        )

    # 「重新生成」：不新增玩家消息，直接重写这一轮的回话（与 _regenerate_turn 同一套判断）
    anchor: Message | None = None
    ephemeral = False
    conversation: Conversation | None = None
    anchor_seq = 0
    anchor_text = ""
    anchor_attachment: int | None = None
    if req.regenerate:
        row = conversations_repo.find_owned(
            db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
        )
        if row is None:
            raise errors.no_conversation()
        conversation, _slug = row
        if conversation.mode == "group":
            raise errors.AppError(
                "regenerate_unsupported",
                "群聊的重新生成走群聊那一轮，不走单聊接口",
                http_status=400,
            )
        plan = _plan_regenerate(db, conversation, req)
        if plan.mode in ("tail", "older"):
            assert plan.anchor is not None
            # commit 之后 ORM 对象会过期，先把要用的值抓下来
            anchor = plan.anchor
            anchor_seq = anchor.seq
            anchor_text = anchor.text
            anchor_attachment = anchor.attachment_id
            if plan.mode == "tail":
                locked = _clear_tail_for_regenerate(db, conversation, anchor_seq, identity)
                if locked is None:
                    raise errors.no_conversation()
                conversation = locked
            else:
                ephemeral = True

    if anchor is None:
        stored = _store_user_message(db, conversation_id, identity, req)
        if stored.duplicate:
            raise errors.AppError(
                "already_stored",
                "这条消息之前已经存过了；重发不重复生成回复",
                http_status=409,
                details={"seq": stored.seq},
            )
        conversation = stored.conversation
        user_seq = stored.seq
        user_text = req.text
        before_seq = stored.seq
        image_data_url = ""
    else:
        user_seq = anchor_seq
        user_text = anchor_text
        before_seq = anchor_seq
        image_data_url = _image_of_attachment(db, anchor_attachment)

    character = _character_of(db, conversation)
    messages = generate.build_messages(
        db,
        conversation,
        character,
        user_text,
        immersive=req.immersive,
        before_seq=before_seq,
        image_data_url=image_data_url,
        # 流式和非流式必须走同一套时间注入，否则网页（默认 SSE）问"几点了"
        # 会完全拿不到时间，只能靠模型猜（2026-11-18 线上实测：说出 11:20）。
        local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
        time_enabled=req.time_enabled,
    )
    conversation_public_id = conversation.public_id

    _check_rate(identity.device)

    # 到这里数据库的活都干完了；连接可以还回去，接下来只有网络
    db.commit()

    async def frames() -> AsyncIterator[bytes]:
        parts: list[str] = []
        try:
            async for chunk in generate.stream_text(
                messages, immersive=req.immersive, think=req.think
            ):
                parts.append(chunk)
                yield _sse("delta", {"text": chunk})
        except Exception as exc:  # noqa: BLE001 —— 流里什么都能抛，统一转成一个 error 帧
            logger.warning("流式生成失败 conv=%s: %s", conversation_public_id, exc)
            yield _sse("error", {"code": "upstream_error", "message": str(exc)[:200]})
            return

        # 更早那一轮的消息不落库（见 _plan_regenerate），这里也不能存回话
        reply_seq = (
            None if ephemeral else _persist_streamed_reply(conversation_public_id, "".join(parts))
        )
        yield _sse("done", {"seq": user_seq, "reply_seq": reply_seq})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # 反代（nginx）看到这个头就不会缓冲 SSE，否则会攒一大坨再一起给你
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/messages", response_model=MessageListOut)
def list_messages(
    conversation_id: str,
    db: DbSession,
    identity: CurrentIdentity,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> MessageListOut:
    """增量拉取消息。客户端下次带 `after_seq=next_after_seq` 接着拉。

    带图的消息会给出 `attachment_id` + `attachment_url`：客户端拿它去取原图。
    没有这两个字段的话，重新进会话时图片消息会变成一条空白气泡 ——
    导入进来的老记录（图片最多）最吃亏。
    """
    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, _slug = row

    rows = messages_repo.list_after_seq(db, conversation.id, after_seq, limit)
    attachment_ids = [item.attachment_id for item in rows if item.attachment_id is not None]
    public_ids = attachments_repo.public_ids_by_id(db, attachment_ids)

    items = []
    for item in rows:
        public_id = public_ids.get(item.attachment_id) if item.attachment_id else None
        items.append(
            MessageOut(
                seq=item.seq,
                side=item.side,
                speaker=item.speaker,
                text=item.text,
                attachment_id=public_id,
                attachment_url=f"/v1/attachments/{public_id}" if public_id else None,
                created_at=to_iso_str(item.created_at),
            )
        )
    next_after_seq = items[-1].seq if items else after_seq
    return MessageListOut(items=items, next_after_seq=next_after_seq)


# ---------------------------------------------------------------- 内部小工具
@dataclass(slots=True)
class _StoredMessage:
    """一条已经落库的用户消息（或一条"这条之前存过"的回放）。"""

    conversation: Conversation
    seq: int
    created_at: datetime
    duplicate: bool
    attachment_id: int | None = None


def _store_user_message(
    db: DbSession, conversation_id: str, identity: Identity, req: MessageCreateRequest
) -> _StoredMessage:
    """把用户消息存进库。

    重复判断做两次（锁前一次、锁后一次）：
      * 锁前那次是为了**省掉不必要的行锁**（绝大多数重发都在这里返回）；
      * 锁后那次才是权威判断 —— 两个请求同时到达时，只有一个能拿到锁并插入。
    """
    attachment = None
    if req.attachment_id:
        attachment = attachments_repo.find_owned(db, req.attachment_id, identity.device_ids)
        if attachment is None:
            raise errors.AppError("no_attachment", "图片不存在", http_status=404)
    if not req.text.strip():
        raise errors.bad_request("text 不能为空")

    row = conversations_repo.find_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if row is None:
        raise errors.no_conversation()
    conversation, _slug = row

    existing = messages_repo.find_by_client_msg_id(db, conversation.id, req.client_msg_id)
    if existing is not None:
        return _StoredMessage(conversation, existing.seq, existing.created_at, True)

    locked = conversations_repo.lock_owned(
        db, conversation_id, user_id=identity.user_id, device_id=identity.device.id
    )
    if locked is None:
        raise errors.no_conversation()
    conversation = locked

    existing = messages_repo.find_by_client_msg_id(
        db, conversation.id, req.client_msg_id, for_update=True
    )
    if existing is not None:
        # 先把它要回放的值读出来，再 rollback —— rollback 会让 ORM 对象过期
        stored = _StoredMessage(conversation, existing.seq, existing.created_at, True)
        db.rollback()  # 释放行锁；什么都没写
        return stored

    message = messages_repo.insert(
        db,
        conversation=conversation,
        client_msg_id=req.client_msg_id,
        side="user",
        text=req.text,
        speaker=req.speaker,
        attachment_id=attachment.id if attachment is not None else None,
    )
    conversations_repo.touch(db, conversation)
    db.commit()
    return _StoredMessage(
        conversation,
        message.seq,
        message.created_at,
        False,
        attachment.id if attachment is not None else None,
    )


def _out_of(stored: _StoredMessage) -> MessageCreatedOut:
    """没有回复的响应（重复回放 / 只存不发）。"""
    return MessageCreatedOut(
        seq=stored.seq,
        duplicate=stored.duplicate,
        reply=None,
        created_at=to_iso_str(stored.created_at),
    )


def _check_rate(device: Device) -> None:
    """每设备每分钟的出话次数。超了回 429，并告诉客户端等几秒。"""
    allowed, retry_after = ratelimit.user_messages().allow(str(device.public_id))
    if not allowed:
        raise errors.AppError(
            "rate_limited",
            f"发得太快了，{retry_after:.0f} 秒后再试",
            http_status=429,
            details={"retry_after": round(retry_after, 1)},
        )


def _image_of(db: DbSession, stored: "_StoredMessage") -> str:
    """把这一轮附带的图转成 data URL。只有当前轮的图会进 prompt（历史图不重发）。"""
    return _image_of_attachment(db, stored.attachment_id)


def _image_of_attachment(db: DbSession, attachment_id: int | None) -> str:
    """同上，但直接给 attachment_id（重新生成时锚点是从库里读出来的消息）。"""
    if not attachment_id:
        return ""
    from app.models import Attachment

    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        return ""
    try:
        return attachments_repo.to_data_url(get_settings().upload_dir, attachment)
    except FileNotFoundError:
        return ""


def _request_id(request: Request) -> str:
    """从中间件塞进 request.state 的 id 取出来（main.py 里每个请求都会生成一个）。"""
    return str(getattr(request.state, "request_id", "") or "")


def _character_of(db: DbSession, conversation: Conversation) -> Character | None:
    if conversation.character_id is None:
        return None
    return characters_repo.find_by_id(db, conversation.character_id)


def _persist_streamed_reply(conversation_public_id: str, text: str) -> int | None:
    """流结束后用**新的会话**把回复落库（此时原来那个 db 已经还回去了）。

    返回回复的 seq；没有内容则返回 None（客户端空回复时不写空消息）。
    """
    if not text.strip():
        return None
    with SessionLocal() as fresh:
        conversation = conversations_repo.find_by_public_id(fresh, conversation_public_id)
        if conversation is None:
            return None
        generation = generate.Generation(text=text)
        reply = generate.store_reply(fresh, conversation, generation)
        generate.touch_conversation(conversation)
        fresh.commit()
        return reply.seq


def _sse(event: str, data: dict[str, object]) -> bytes:
    r"""拼一个 SSE 帧。

    `ensure_ascii=False` 保证中文原样发出去，而不是变成 \u4f60\u597d 这种转义序列
    （不转义省带宽、抓包时也看得懂）。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _weights_of(
    db: DbSession,
    owner: str,
    member_rows: list[tuple[int, str, str, int]],
) -> list[MemberWeightOut]:
    """把群成员行算成「有效权重」列表，顺序与 `members` 一一对应。

    有效值口径和群聊选人（`app/group.py::_weights`）完全一致：
    **玩家在 character_weights 里覆盖过就用覆盖值，否则用角色自带 active_weight。**
    这样"详情页显示的权重"和"实际选人用的权重"是同一个数，不会出现
    「界面写 80、抽人却按 50」这种对不上的情况。
    """
    if not member_rows:
        return []
    defaults = characters_repo.default_weights(
        db, [character_id for character_id, _slug, _name, _ord in member_rows]
    )
    effective = memories_repo.effective_weights(db, owner_key=owner, defaults=defaults)

    items: list[MemberWeightOut] = []
    for character_id, slug, name, _ord in member_rows:
        weight, source = effective.get(character_id, (50, "default"))
        items.append(
            MemberWeightOut(
                character_id=slug,
                name=name,
                weight=weight,
                weight_source=source,
            )
        )
    return items


def _to_out(
    conversation: Conversation,
    character_slug: str | None,
    members: list[str] | None = None,
    member_weights: list[MemberWeightOut] | None = None,
) -> ConversationOut:
    """把 ORM 对象转成对外形状。会话的 `character_id` 对外就是 slug。"""
    return ConversationOut(
        id=conversation.public_id,
        character_id=character_slug,
        mode=conversation.mode,
        title=conversation.title,
        next_seq=conversation.next_seq,
        speak_mode=conversation.speak_mode,
        memory_mode=conversation.memory_mode,
        members=members or [],
        member_weights=member_weights or [],
        created_at=to_iso_str(conversation.created_at),
        last_active_at=to_iso_str(conversation.last_active_at),
    )
