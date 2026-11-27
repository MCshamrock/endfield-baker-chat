"""api/legacy_group.py —— 老客户端群聊（`/group/*`）兼容层。

老客户端的群聊是**另一套协议**（独立服务 `group_chat_service`，5810 端口），
它不建会话、不落库，把整段 history 一起发上来，服务端只负责"这次谁说话、说什么"。
这一层的任务就是让它原样跑在新后端上，字段一个不少。

六个端点（字段与老服务逐字对齐，见 docs/06-老群聊协议.md）：

    POST /group/session          建群（返回 session 快照 + ttl）
    POST /group/reply            取下一步回复（round 一次给全员 / smart 一条 / assign 指定的人）
    POST /group/suggestions      输入推荐（玩家点开面板时才拉）
    POST /group/topic            改话题
    POST /group/session/close    关群
    GET  /group/sessions         调试用：当前有哪些会话

三个和老服务不一样、但对老客户端无感的地方：

1. **会话状态在内存里**，不在 SQLite。老服务多 worker 才需要外置存储；我们是单进程，
   一张带 TTL 的字典就够，而且少一个文件少一种坏法。
2. **提示词换成新后端那一套**（世界书预取 + 群聊分层），回答质量比老服务好，
   但响应结构完全不变。
3. **不记用量**。老契约没有 token 上限概念，硬记会污染账单；真要看用量去 /v1 那边。

`round` 模式一次要吐**全部成员各一条**，这是老协议里唯一"一次请求多次调用上游"的地方 ——
必须照做，否则老客户端会以为有人没说话。
"""

import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import group, suggestions, upstream
from app.clock import local_time_text
from app.deps import DbSession
from app.models import Character
from app.prompt.group import build_group_messages, build_transcript
from app.repos import characters as characters_repo
from app.repos import content as content_repo
from app.repos import memories as memories_repo
from app.repos import worldbook as worldbook_repo
from app.retrieval.store import prefetch

router = APIRouter(tags=["legacy-group"])
logger = logging.getLogger("baker.legacy.group")

# 与老服务同名同值，改这里等于改协议
SESSION_TTL = 300
MAX_TURNS = 60
# 同一会话的**累计**轮数上限（永不因 new_turn 复位）。老服务默认是 MAX_TURNS 的 10 倍。
# 为什么还要它：MAX_TURNS 是"本次连续推进"的计数，玩家点「恢复对话」时前端会带
# new_turn=true 把它清零；没有这层只增不减的护栏，无鉴权的客户端每次带 new_turn
# 就能无限刷上游。正常玩家碰不到 600。
MAX_TOTAL_TURNS = 600
MAX_SESSIONS = 200
HISTORY_LIMIT = 50
ROLE_OBSERVER = "observer"
ROLE_ADMIN = "admin"
# 老客户端只会发前三个；weighted 是给新版留的口子（老客户端永远不会送来）
VALID_SPEAK_MODES = ("round", "smart", "assign", "weighted")

# smart 模式里主持人判"该停了"时**不收摊**，改用这段续接指令把话题接下去。
# 文案逐字取自老服务 group_chat_service.py 的 NUDGE_CONTINUE —— 玩家看到的行为
# 必须和老后端一致：只有玩家自己点暂停，群聊才会停。
NUDGE_CONTINUE = (
    "（群里刚安静了一小会儿。请不要总结、不要道别,自然地接一句把话题续下去:"
    "可以回应上文某个细节、可以提出一个相关的实际问题、也可以抛出新的引子。"
    "保持你一贯的语气,像平时在群里那样。）"
)

# ---------------------------------------------------------------- 会话表
class _Session:
    """一次群聊的运行时快照。字段名与老服务的 to_public 一致。"""

    __slots__ = (
        "session_id",
        "members",
        "my_role",
        "speak_mode",
        "assign_target",
        "topic",
        "group_name",
        "last_speaker",
        "turns",
        "total_turns",
        "expires_at",
    )

    def __init__(self, session_id: str, members: list[str], my_role: str, speak_mode: str) -> None:
        self.session_id = session_id
        self.members = members
        self.my_role = my_role
        self.speak_mode = speak_mode
        self.assign_target = members[0] if members else ""
        self.topic = ""
        self.group_name = ""
        self.last_speaker = ""
        self.turns = 0
        self.total_turns = 0
        self.expires_at = time.time() + SESSION_TTL

    def touch(self) -> None:
        self.expires_at = time.time() + SESSION_TTL

    def ai_members(self) -> list[str]:
        """能被 AI 接管的成员：玩家自己扮演的那个要排掉。"""
        return [name for name in self.members if name != self.my_role]

    def to_public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "members": self.members,
            "my_role": self.my_role,
            "speak_mode": self.speak_mode,
            "assign_target": self.assign_target,
            "topic": self.topic,
            "group_name": self.group_name,
            "ttl": SESSION_TTL,
        }


_SESSIONS: dict[str, _Session] = {}


def _reap() -> None:
    now = time.time()
    for key in [k for k, s in _SESSIONS.items() if s.expires_at < now]:
        _SESSIONS.pop(key, None)


def _get(session_id: str) -> _Session | None:
    session = _SESSIONS.get(session_id)
    if session is None:
        return None
    if session.expires_at < time.time():
        _SESSIONS.pop(session_id, None)
        return None
    session.touch()
    return session


def _err(message: str, status: int = 400, *, code: str = "", **extra: Any) -> JSONResponse:
    """老协议的错误体：`{"error": ..., "code": ..., 附加字段}`。

    `code` 是给前端做分支用的（老前端不读它，但它能让我们自己的客户端少做正则匹配）。
    """
    body: dict[str, Any] = {"error": message}
    if code:
        body["code"] = code
    body.update(extra)
    return JSONResponse(body, status_code=status)


# ---------------------------------------------------------------- 请求体
class CreateSessionRequest(BaseModel):
    members: list[str] = Field(default_factory=list)
    my_role: str = ROLE_ADMIN
    speak_mode: str = "smart"
    assign_target: str = ""
    topic: str = ""
    group_name: str = ""


class HistoryItem(BaseModel):
    side: str = "other"  # "mine" | "other"
    text: str = ""
    speaker: str = ""
    image: str = ""


class ReplyRequest(BaseModel):
    session_id: str
    history: list[HistoryItem] = Field(default_factory=list)
    topic: str = ""
    force_search: bool = False
    think: bool = False
    immersive_mode: bool = True
    new_turn: bool = False
    members: list[str] | None = None
    my_role: str | None = None
    speak_mode: str | None = None
    assign_target: str | None = None
    use_new_prompt: bool = False
    use_rag: bool | None = None
    want_suggestions: bool = True
    local_time: str = ""
    # 「时间感知」开关。老客户端不传 → 默认开，行为与改动前完全一致
    time_enabled: bool = True


class SuggestionsRequest(BaseModel):
    session_id: str
    history: list[HistoryItem] = Field(default_factory=list)
    topic: str = ""


class TopicRequest(BaseModel):
    session_id: str
    topic: str = ""


class CloseRequest(BaseModel):
    session_id: str


# ---------------------------------------------------------------- 建群 / 关群
@router.post("/group/session")
def create_session(req: CreateSessionRequest) -> Any:
    """建一个群聊会话。成员少于 2 个、模式不认识都按老服务的方式拒绝。"""
    _reap()
    members: list[str] = []
    for name in req.members:
        clean = (name or "").strip()
        if clean and clean not in members:
            members.append(clean)
    if len(members) < 2:
        return _err("群聊至少需要 2 名成员", 400, code="bad_members")
    if req.speak_mode not in VALID_SPEAK_MODES:
        return _err(f"speak_mode 必须是 {VALID_SPEAK_MODES} 之一", 400, code="bad_speak_mode")
    if len(_SESSIONS) >= MAX_SESSIONS:
        return _err(
            f"群聊会话数已达上限 {MAX_SESSIONS}，请稍后重试或关闭闲置会话",
            429,
            retry_after=30,
            code="session_limit",
        )

    my_role = (req.my_role or ROLE_ADMIN).strip()
    if my_role not in (ROLE_OBSERVER, ROLE_ADMIN) and my_role not in members:
        return _err(
            f"my_role 必须是 {ROLE_OBSERVER}/{ROLE_ADMIN} 或成员之一",
            400,
            code="bad_my_role",
        )

    session = _Session(_new_id(), members, my_role, req.speak_mode)
    session.topic = (req.topic or "").strip()
    session.group_name = (req.group_name or "").strip()
    target = (req.assign_target or "").strip()
    session.assign_target = target if target in members else members[0]
    _SESSIONS[session.session_id] = session
    logger.info("老群聊建群 %s members=%s role=%s", session.session_id, members, my_role)
    return {"ok": True, **session.to_public()}


@router.post("/group/session/close")
def close_session(req: CloseRequest) -> Any:
    closed = _SESSIONS.pop(req.session_id, None)
    return {"ok": True, "closed": closed is not None}


@router.get("/group/sessions")
def list_sessions() -> Any:
    """调试用：当前有哪些会话。不含任何密钥，可以放心开着。"""
    _reap()
    now = time.time()
    return {
        "count": len(_SESSIONS),
        "sessions": [
            {
                "session_id": item.session_id,
                "members": item.members,
                "my_role": item.my_role,
                "speak_mode": item.speak_mode,
                "assign_target": item.assign_target,
                "last_speaker": item.last_speaker,
                "turns": item.turns,
                "total_turns": item.total_turns,
                "idle_s": round(now + SESSION_TTL - item.expires_at, 1),
            }
            for item in _SESSIONS.values()
        ],
    }


@router.post("/group/topic")
def set_topic(req: TopicRequest) -> Any:
    session = _get(req.session_id)
    if session is None:
        return _session_expired()
    session.topic = (req.topic or "").strip()
    return {"ok": True, "session_id": session.session_id, "topic": session.topic}


# ---------------------------------------------------------------- 发言
@router.post("/group/reply")
async def group_reply(
    req: ReplyRequest,
    db: DbSession,
    authorization: Annotated[str, Header()] = "",
) -> Any:
    """取"下一步"的回复。

    round  → 全员各一条，done=True
    assign → 指定那位一条，done=True
    smart  → 一条，done 表示"该停了"（前端据此结束循环）

    `authorization` 是**可选**的：老客户端不会带 token，那就按角色默认权重；
    新客户端带了 token，weighted 模式就能用上这个玩家自己设的覆盖值。
    """
    session = _get(req.session_id)
    if session is None:
        return _session_expired()

    if req.topic.strip():
        session.topic = req.topic.strip()
    _apply_config(session, req)
    if req.new_turn:
        session.turns = 0

    # 认不出就返回 None（不是 401）：老群聊协议从来不需要登录
    owner = characters_repo.optional_owner_key(db, authorization)
    history = list(req.history)
    try:
        return await _run_round(db, session, history, req, owner)
    except upstream.UpstreamError as exc:
        return _upstream_error(exc)


@router.post("/group/suggestions")
async def group_suggestions(req: SuggestionsRequest, db: DbSession) -> Any:
    """输入推荐（玩家点开面板时才拉，老客户端为省调用会传 want_suggestions=False）。"""
    session = _get(req.session_id)
    if session is None:
        return _session_expired()
    if req.topic.strip():
        session.topic = req.topic.strip()

    if session.my_role == ROLE_OBSERVER:
        return {"ok": True, "session_id": session.session_id, "suggestions": []}

    items = await _suggestions(db, session, list(req.history), session.topic)
    return {"ok": True, "session_id": session.session_id, "suggestions": items}


def _stop_payload(
    session: _Session,
    *,
    stop_reason: str,
    stop_code: str,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """各种"这一轮不出话"的停止响应（形状统一，老客户端只读 stop_code/reason）。"""
    return {
        "ok": True,
        "session_id": session.session_id,
        "speak_mode": session.speak_mode,
        "replies": [],
        "suggestions": suggestions or [],
        "done": True,
        "last_speaker": session.last_speaker,
        "stop_reason": stop_reason,
        "stop_code": stop_code,
    }


def _total_turns_stop(session: _Session) -> dict[str, Any] | None:
    """累计轮数护栏（只增不减，new_turn 也清不掉）。没超就返回 None。"""
    if session.total_turns < MAX_TOTAL_TURNS:
        return None
    logger.warning(
        "老群聊 %s 累计 %s 轮，超过上限 %s，拒绝继续生成",
        session.session_id,
        session.total_turns,
        MAX_TOTAL_TURNS,
    )
    return _stop_payload(
        session,
        stop_reason=f"本群聊累计发言已达上限 {MAX_TOTAL_TURNS} 轮，请新建群聊继续",
        stop_code="max_total_turns",
    )


async def _no_speaker_stop(
    db: DbSession, session: _Session, history: list[HistoryItem], req: ReplyRequest
) -> dict[str, Any]:
    """这一轮没人能开口：区分"玩家自己扮演"和"群里没有 AI 成员"两种原因。"""
    if session.speak_mode == "assign" and session.assign_target in session.members:
        target = session.assign_target
        # 老服务这条分支照样给输入推荐（玩家还是要打字），所以这里也照做。
        suggestions = (
            await _suggestions(db, session, history, req.topic)
            if req.want_suggestions and session.my_role != ROLE_OBSERVER
            else []
        )
        return _stop_payload(
            session,
            stop_reason=f"「{target}」由玩家本人扮演,请直接输入该角色的发言",
            stop_code="player_role",
            suggestions=suggestions,
        )
    return _stop_payload(
        session, stop_reason="没有可由 AI 接管的成员", stop_code="no_ai_member"
    )


async def _safe_member_reply(
    db: DbSession,
    session: _Session,
    name: str,
    history: list[HistoryItem],
    req: ReplyRequest,
) -> dict[str, Any] | None:
    """round 模式用：一个成员失败只回一条空占位，别把整轮带崩（老服务行为）。

    例外：429 是"整个上游都限速了"，换人也没用 —— 照老服务往上抛，
    由端点回正经的限速错误（_upstream_error 会把它翻成 429 + retry_after）。
    """
    try:
        return await _one_reply(db, session, name, history, req)
    except upstream.UpstreamError as exc:
        if session.speak_mode != "round" or exc.status == 429:
            raise
        logger.warning("老群聊 round：%s 生成失败，跳过该成员：%s", name, exc)
        return {
            "speaker": name,
            "text": "",
            "need_search": False,
            "knowledge_source": "none",
            "usage": {},
            # 老服务的占位只有 speaker/text/error 三个键；这里多带几个
            # 是为了让"每条 reply 形状一致"，老客户端读不到也不影响
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _run_round(
    db: DbSession,
    session: _Session,
    history: list[HistoryItem],
    req: ReplyRequest,
    owner: str | None = None,
) -> Any:
    """按模式产出一条或多条回复。"""
    # ---- 总量护栏（只增不减，new_turn 也清不掉）----
    # 老服务在这里返回 done=True + stop_code，而不是 429：
    # 这不是"你请求太快"，而是"这个群聊已经聊到头了，请新建一个"。
    total_stop = _total_turns_stop(session)
    if total_stop is not None:
        return total_stop

    # `owner` 只有 weighted 分支会用到（玩家覆盖权重）；其它模式传不传都一样
    speakers = _speakers_for(session, db, owner)
    if not speakers:
        return await _no_speaker_stop(db, session, history, req)

    replies: list[dict[str, Any]] = []
    done = True
    stop_reason = ""
    stop_code = ""

    for name in speakers:
        if session.turns >= MAX_TURNS:
            done = True
            stop_reason = f"已达到单次连续轮数上限 {MAX_TURNS}，点「恢复对话」可继续"
            stop_code = "max_turns"
            break
        reply = await _safe_member_reply(db, session, name, history, req)
        if reply is None:  # smart 模式下没有可接话的 AI 成员
            done = True
            break
        replies.append(reply)
        # 用 reply 里**真正开口的那位**，不能用循环变量 name：
        # smart 模式下 _one_reply 会让主持人改选（name 只是候选名单里的第一个），
        # 写成 name 就会出现「回复是黎风说的、last_speaker 记成佩丽卡」——
        # 下一轮 prompt 里的「上一条是谁说的」也会跟着错（实测 2026-11-17）。
        session.last_speaker = str(reply.get("speaker") or name)
        session.turns += 1
        session.total_turns += 1
        if session.speak_mode in ("smart", "weighted") and session.turns > 0:
            # 智能模式一次一条，由前端接着请求；只有 round 才一次给全员
            done = False
            break

    suggestions = (
        await _suggestions(db, session, history, req.topic)
        if req.want_suggestions and session.my_role != ROLE_OBSERVER
        else []
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "speak_mode": session.speak_mode,
        "replies": replies,
        "suggestions": suggestions,
        "done": done,
        "last_speaker": session.last_speaker,
        "stop_reason": stop_reason,
        "stop_code": stop_code,
    }


def _speakers_for(
    session: _Session, db: DbSession, owner: str | None = None
) -> list[str]:
    """这一轮该由谁说话（顺序即返回顺序）。"""
    candidates = session.ai_members()
    if not candidates:
        return []
    if session.speak_mode == "assign":
        target = session.assign_target or candidates[0]
        # 目标不在 AI 可接管的名单里（= 玩家自己扮演的角色）就返回空，
        # 由 _run_round 回 player_role 的停止原因；**绝不换个人顶替**。
        return [target] if target in candidates else []
    if session.speak_mode == "weighted":
        # 老客户端不会送 weighted（这是给新客户端留的分支），所以这条不影响老行为。
        # 权重必须和 /v1 群聊同源：玩家覆盖 > 角色自带 active_weight，
        # 否则同一个群里「两条链路」抽出来的人会不一样。
        member_rows, weights = _weighted_rows(db, session, owner)
        picked = group.choose_speaker(member_rows, weights=weights, spoken=set())
        return [picked[1]] if picked is not None else candidates[:1]
    # round 一次给全员；smart 先给第一个，真正的选择在 _one_member 里由主持人决定
    return candidates


async def _one_reply(
    db: DbSession,
    session: _Session,
    speaker: str,
    history: list[HistoryItem],
    req: ReplyRequest,
) -> dict[str, Any] | None:
    """让某位说一句。返回 None 只表示"群里没有 AI 可接话的成员"。

    smart 模式先问主持人；主持人判 END 时**不收摊**（那是老服务早年的行为），
    而是换成"上一位的下一位"把话题自然续下去，并附带 NUDGE_CONTINUE。
    """
    nudge = ""
    if session.speak_mode == "smart":
        chosen = await _judge_next(db, session, history)
        if chosen is None:
            members = session.ai_members()
            if not members:
                return None
            # 环状取"上一位发言者的下一位"，保证不会同一个人连说
            try:
                at = members.index(session.last_speaker)
            except ValueError:
                at = -1
            speaker = members[(at + 1) % len(members)]
            nudge = NUDGE_CONTINUE
            logger.info("老群聊 smart 判 END，按老服务改为续接：轮到 %s", speaker)
        else:
            speaker = chosen

    messages, player_text = _build_messages(db, session, speaker, history, req, nudge=nudge)
    generation = await upstream_call(db, messages, immersive=req.immersive_mode, think=req.think)
    session.last_speaker = speaker
    return {
        "speaker": speaker,
        "text": generation.text,
        "need_search": False,
        "knowledge_source": "worldbook" if player_text else "none",
        "usage": {
            "prompt_tokens": generation.prompt_tokens,
            "completion_tokens": generation.completion_tokens,
            "cached_tokens": generation.cached_tokens,
            "total_ms": generation.total_ms,
        },
    }


def _build_messages(
    db: DbSession,
    session: _Session,
    speaker: str,
    history: list[HistoryItem],
    req: ReplyRequest,
    *,
    nudge: str = "",
) -> tuple[list[dict[str, object]], str]:
    """把老协议的 history 翻成新后端的群聊提示词。

    老协议最后一条通常是玩家刚说的话（side=mine）。它要当"本轮输入"，
    不能留在历史里 —— 否则同一句话会出现两遍，模型会当成玩家说了两次。
    """
    entries = history[-HISTORY_LIMIT:]
    player_text = ""
    if entries and entries[-1].side == "mine":
        player_text = _history_text(entries[-1])
        entries = entries[:-1]

    pairs = [
        (item.speaker or ("玩家" if item.side == "mine" else ""), _history_text(item))
        for item in entries
        if _history_text(item)
    ]
    transcript = build_transcript(pairs)

    character = _find_character(db, speaker)
    # 群聊也认老客户端的 use_new_prompt 开关：同一个请求里的**每个发言人**
    # 都必须用同一个 variant（不能这轮佩丽卡用 v2、下一条陈千语用 v1）。
    # round 模式一次要给全员各一条，variant 由 req 决定，所以循环里每次算都一样。
    variant = "v2" if req.use_new_prompt else "v1"
    character_prompt = (
        content_repo.get_character_prompt(db, character.id, variant=variant)
        if character is not None
        else ""
    )
    recent = [_history_text(item) for item in entries if item.side == "mine"]
    context = _worldbook_context(db, recent, player_text)

    messages = build_group_messages(
        safety_protocol=content_repo.get_safety_protocol(db),
        worldview=content_repo.get_text_config(db, "worldview"),
        speaker=speaker,
        character_prompt=character_prompt,
        members=session.members,
        player_role=session.my_role,
        group_name=session.group_name,
        transcript=transcript,
        summary="",
        memory="",
        context=context,
        nudge=nudge,
        player_message=player_text,
        immersive=req.immersive_mode,
        local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
        time_enabled=req.time_enabled,
    )
    return messages, player_text


def _history_text(item: HistoryItem) -> str:
    """一条群聊历史真正要送给模型的文字。

    老协议里**图片是挂在 history 项上的**（`image` 字段），不是多模态内容。
    纯图片那条的 text 是空的，直接过滤掉的话模型就"看不见玩家发了图"。
    老服务的文案（逐字照抄，别改）：
      * 别人发的图 → `[图片]`
      * 玩家发的图 → `[玩家发来一张图片]`
    """
    text = item.text.strip()
    if text:
        return text
    if item.image.strip():
        return "[图片]" if item.side == "other" else "[玩家发来一张图片]"
    return ""


def _worldbook_context(db: DbSession, recent: list[str], player_text: str) -> str:
    """世界书预取：和老服务一样，按最近几句话去命中资料。"""
    entries = worldbook_repo.list_entries(db, "endfield")
    if not entries:
        return ""
    return prefetch(entries, [*recent[-2:], player_text])


async def _judge_next(
    db: DbSession, session: _Session, history: list[HistoryItem]
) -> str | None:
    """smart 模式：让主持人决定下一位，返回 None 表示主持人判了 END。"""
    pairs = [
        (item.speaker or ("玩家" if item.side == "mine" else ""), _history_text(item))
        for item in history[-HISTORY_LIMIT:]
        if _history_text(item)
    ]
    members = _member_rows(session.ai_members())
    if not members:
        return None
    picked = await group.pick_speaker(
        db, members=members, transcript=build_transcript(pairs), last_speaker=session.last_speaker
    )
    return None if picked is None else picked[1]


def _member_rows(names: list[str]) -> list[tuple[int, str, str, int]]:
    """把"中文名列表"包成 group.choose_speaker 需要的形状（我们只有名字，没有 id）。"""
    return [(index, name, name, index) for index, name in enumerate(names)]


def _weighted_rows(
    db: DbSession, session: _Session, owner: str | None
) -> tuple[list[tuple[int, str, str, int]], dict[int, int]]:
    """weighted 模式专用：给每位成员配上**真实角色 id** 和**有效权重**。

    老协议里成员只有中文名、没有 slug/id，所以这里按名字回查 `characters` 表：
    有了真实 id，才能和玩家的覆盖表（`character_weights.character_id`）对上。

    两种兜底：
      * `owner=None`（老客户端没带 token）→ 不查覆盖表，全部用角色自带 active_weight；
      * 名字在库里找不到（比如玩家自建角色）→ 用一个**负数假 id** 兜底，
        权重按 50（和 `choose_speaker` 的默认值一致）。真 id 都是正数，不会撞。
    """
    names = session.ai_members()
    if not names:
        return [], {}

    rows = db.scalars(select(Character).where(Character.name.in_(names))).all()
    by_name = {row.name: row for row in rows}
    defaults = {row.id: row.active_weight for row in by_name.values()}
    effective = memories_repo.effective_weights(db, owner_key=owner, defaults=defaults)

    member_rows: list[tuple[int, str, str, int]] = []
    weights: dict[int, int] = {}
    for index, name in enumerate(names):
        character = by_name.get(name)
        if character is None:
            fake_id = -(index + 1)
            member_rows.append((fake_id, name, name, index))
            weights[fake_id] = 50
            continue
        member_rows.append((character.id, name, name, index))
        weights[character.id] = effective[character.id][0]
    return member_rows, weights


def _find_character(db: DbSession, name: str) -> Character | None:
    return db.query(Character).filter(Character.name == name).one_or_none()


async def _suggestions(
    db: DbSession, session: _Session, history: list[HistoryItem], topic: str
) -> list[str]:
    """输入推荐：和单聊/新客户端共用同一段提示词，免得三个入口三种风格。"""
    transcript = build_transcript(
        [
            (item.speaker or ("玩家" if item.side == "mine" else ""), _history_text(item))
            for item in history[-HISTORY_LIMIT:]
            if _history_text(item)
        ]
    )
    try:
        return await suggestions.suggest_for_group(
            db,
            members=session.members,
            transcript=transcript,
            identity_note=session.my_role,
            topic=topic or session.topic,
        )
    except Exception as exc:  # noqa: BLE001 —— 推荐失败绝不能把整轮群聊变成 500
        logger.warning("老群聊推荐失败，按老服务返回空列表：%s", exc)
        return []


async def upstream_call(
    db: DbSession, messages: list[dict[str, object]], *, immersive: bool, think: bool
) -> Any:
    """统一走 key 池。放一层是因为推荐和正片都要调，别各写一份。"""
    from app import generate

    return await generate.one_shot(db, messages, immersive=immersive, think=think)


def _apply_config(session: _Session, req: ReplyRequest) -> None:
    """把请求里带的会话配置落到快照上。

    老前端不带这些字段，行为与以前逐字一致；带了的就地生效（玩家"指定换人"后
    不用重建群才认）。四个字段各自一段，规则见 docs/06 §3.2。
    """
    _apply_members(session, req.members)
    _apply_role(session, req.my_role)
    if req.speak_mode is not None and req.speak_mode in VALID_SPEAK_MODES:
        session.speak_mode = req.speak_mode
    _apply_target(session, req.assign_target)


def _apply_members(session: _Session, members: list[str] | None) -> None:
    """成员变更：少于 2 人一律忽略（前端还在编辑时不要清空群）。"""
    if members is None:
        return
    cleaned: list[str] = []
    for name in members:
        value = (name or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if len(cleaned) >= 2:
        session.members = cleaned
    else:
        logger.warning("忽略成员变更（只有 %s 人）", len(cleaned))


def _apply_role(session: _Session, role_value: str | None) -> None:
    if role_value is None:
        return
    role = (role_value or "").strip()
    if role and (role in (ROLE_OBSERVER, ROLE_ADMIN) or role in session.members):
        session.my_role = role


def _apply_target(session: _Session, target_value: str | None) -> None:
    """指定发言人：只接受在群里的名字；模式是 assign 时还得保证目标合法。"""
    if target_value is not None:
        target = (target_value or "").strip()
        if target in session.members:
            session.assign_target = target
    if session.speak_mode == "assign" and session.assign_target not in session.members:
        session.assign_target = session.members[0] if session.members else ""


def _session_expired() -> JSONResponse:
    return _err(
        "会话不存在或已超时,请重新创建群聊",
        404,
        session_expired=True,
        code="session_expired",
    )


def _upstream_error(exc: upstream.UpstreamError) -> JSONResponse:
    if exc.status == 429:
        return _err("上游 AI 限速,请稍后重试", 429, retry_after=20, code="rate_limit")
    return _err(f"群聊生成失败: {exc}", 502, code="upstream_error")


def _new_id() -> str:
    return uuid.uuid4().hex


__all__ = ["router"]
