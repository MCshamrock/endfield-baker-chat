"""api/legacy.py —— 老客户端兼容层（3.0 – 3.2.2 的 `/chat` 与 `/chat/stream`）。

老客户端长什么样（读 `frontend-src/v322kit/src/utils/backend.ts` 确认）：
  * 只发 `Content-Type: application/json`，**没有 token、没有设备 id**；
  * 每次把最近几十轮 history 一起传上来（它自己就是数据库）；
  * 期望的响应字段是 `reply` / `mood` / `usage` / `knowledge_source`；
  * 流式用 SSE，帧名只有 `delta` 和 `done`。

所以这一层是**无状态**的：不建会话、不落库，按请求里的 history 现场拼提示词。
这不是妥协 —— 老客户端本来就是这么工作的（翻转只发生在新客户端）；
等它升级到会带 token 的版本，自然走 /v1 那套有状态的链路。

`/group/*` 不在这里 —— 老的群聊是另一套独立协议（建群/发言/推荐/关群），
实现单独放在 `api/legacy_group.py`，字段与老服务逐字对齐。
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import errors, generate, suggestions, upstream
from app.clock import local_time_text, utcnow
from app.deps import DbSession
from app.models import Character
from app.prompt.dialog import build_dialog_messages
from app.repos import admin as admin_repo
from app.repos import content as content_repo
from app.repos import worldbook as worldbook_repo
from app.retrieval.store import build_index_text, prefetch
from app.settings import get_settings

logger = logging.getLogger("baker.legacy")

router = APIRouter(tags=["legacy"])


class LegacyHistory(BaseModel):
    role: str = "user"
    content: str = ""
    # 图片：老客户端把它放在**本轮那条 history** 里（data URL，前端已降采样）。
    # 以前这个字段被 pydantic 直接丢掉 —— 图发上来了、模型却看不见（2026-11-17 实测）。
    image: str = ""


class LegacyChatRequest(BaseModel):
    """老契约的请求体（只收我们会用到的字段，其余忽略）。"""

    character_id: str = ""
    character: str = ""
    # 老客户端会带这两种"整段提示词"，新后端以前直接丢掉了（pydantic 默认忽略未知字段）：
    #   character_prompt —— 用户在客户端里改过的整段人设（优先级最高）
    #   system_prompt    —— 旧前端给"未收录角色"准备的整段兜底
    character_prompt: str = ""
    system_prompt: str = ""
    message: str = ""
    history: list[LegacyHistory] = Field(default_factory=list)
    immersive_mode: bool = True
    think: bool = False
    # 老前端的两个知识源开关。新后端**不联网**，收下来只为记一条日志（见 docs/12）
    use_rag: bool | None = None
    force_search: bool = False
    use_new_prompt: bool = False
    summary_context: str = ""
    # 客户端那边的时间（可选）。老客户端不传，服务端按东八区补一个
    local_time: str = ""
    # 「时间感知」开关。老客户端不传 → 默认开，行为与改动前完全一致
    time_enabled: bool = True


class LegacyChatResponse(BaseModel):
    """老契约的响应体。多给的字段老客户端会忽略，但不能少。"""

    reply: str
    mood: str = ""
    usage: dict[str, object] = Field(default_factory=dict)
    knowledge_source: str = "none"


def _find_character(db: DbSession, req: LegacyChatRequest) -> Character | None:
    """老客户端可能传 slug，也可能传中文名 —— 两种都认。"""
    slug = req.character_id.strip()
    if slug:
        character = db.query(Character).filter(Character.slug == slug).one_or_none()
        if character is not None:
            return character
    name = (req.character or req.character_id).strip()
    if name:
        return db.query(Character).filter(Character.name == name).one_or_none()
    return None


def _resolve_character(db: DbSession, req: LegacyChatRequest) -> Character | None:
    """找这个请求该用哪个角色**卡**；找不到但前端自带整段人设时返回 None。

    老后端的取词优先级（从高到低）：
      1. `character_prompt` —— 用户在客户端里改过的整段人设；
      2. `characters` 表里的角色卡（v1，或 use_new_prompt=True 时的 v2）；
      3. `system_prompt`   —— 旧前端给"未收录角色"准备的整段兜底。

    注意第 3 条的语义：**没收录的角色不是错误**。只要前端给了兜底提示词，
    就该照常出话，不能 404 —— 老客户端里有一批自建角色全靠这条路。
    只有三种都没有时才是真的"没有这个角色"。
    """
    character = _find_character(db, req)
    if character is not None:
        return character
    if req.character_prompt.strip() or req.system_prompt.strip():
        return None
    raise errors.AppError(
        "no_character",
        f"没有这个角色：{req.character_id or req.character}",
        http_status=404,
    )


def _current_image(req: LegacyChatRequest) -> tuple[str, list[LegacyHistory]]:
    """取出本轮要发的图，并把这条 history 摘出去。

    老客户端的约定：**图片只挂在本轮那条 history 上**，content 是「[图片]」占位，
    之前各轮即使有图也一并以 `image` 字段重传。所以这里只认最后一条：

    * 认最后一条 = 只有刚发的那张图会进 prompt；
    * 不认前面几条 = 历史图不会被每轮重发（既浪费 token，也会让角色反复提那张旧图）；
    * 摘掉这条 = 否则拼提示词时会多出一次「[图片]」的纯文本轮次。
    """
    if not req.history:
        return "", req.history
    last = req.history[-1]
    image = last.image.strip()
    if not image.startswith("data:image/"):
        return "", req.history
    return image, req.history[:-1]


def _build_messages(
    db: DbSession, req: LegacyChatRequest, character: Character | None
) -> list[dict[str, object]]:
    """按新链路同样的分层拼提示词（世界观 / 世界书 / 角色 / 历史 / 本轮）。"""
    settings = get_settings()
    image, history_items = _current_image(req)
    history = [
        {"role": item.role if item.role in ("user", "assistant") else "user", "content": item.content}
        for item in history_items
        if item.content.strip()
    ]

    # 带图那一轮，文字部分通常只有「[图片]」这个占位：把它当正文发过去，
    # 模型容易把占位符复述出来。图已经在下面作为多模态内容挂上了，正文留空即可。
    message = "" if image and req.message.strip() in ("", "[图片]") else req.message

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
            recent = [item["content"] for item in history if item["role"] == "user"][-2:]
            context = prefetch(
                entries, [*recent, req.message], budget_tokens=settings.worldbook_budget_tokens
            )

    # 老后端的取词优先级（和 _resolve_character 的说明对应）：
    #   ① character_prompt（用户改过的整段卡）—— 用了它就不再拼库里的卡；
    #   ② 库里的角色卡（v1 / use_new_prompt 打开时 v2）；
    #   ③ system_prompt（旧前端给未收录角色的整段兜底）。
    # ①③ 都是"前端已经给全了"的整段人设，所以要告诉组装层别再补 EMPHASIS_RULE
    # —— 那会往用户的卡里塞一段他没写过的规则。
    prompt_is_final = False
    if req.character_prompt.strip():
        character_prompt = req.character_prompt
        prompt_is_final = True
    elif character is not None:
        # 老客户端的「使用新提示词」开关（use_new_prompt）：
        #   打开 → 取 v2（characters_v2/ 里的实验性长卡）；
        #   关着 → 取 v1（老包常规长卡，默认）。
        # v2 缺失时由 repo 层回落到 v1（并记 debug 日志），这里不用判断。
        variant = "v2" if req.use_new_prompt else "v1"
        character_prompt = content_repo.get_active_prompt(db, character.id, variant)
    else:
        character_prompt = req.system_prompt
        prompt_is_final = True

    return build_dialog_messages(
        safety_protocol=content_repo.get_safety_protocol(db),
        worldview=content_repo.get_text_config(db, "worldview"),
        worldbook_index=worldbook_index,
        worldbook_constants=worldbook_constants,
        character_prompt=character_prompt,
        character_prompt_is_final=prompt_is_final,
        summary=req.summary_context,  # 老客户端把自己的"智能总结"放在这
        context=context,
        history=history,
        message=message,
        image_data_url=image,
        immersive=req.immersive_mode,
        local_time=(req.local_time.strip() or local_time_text()) if req.time_enabled else "",
        time_enabled=req.time_enabled,
    )


@router.post("/chat", response_model=LegacyChatResponse)
async def legacy_chat(req: LegacyChatRequest, db: DbSession) -> LegacyChatResponse:
    """老契约单聊。无状态：只回答，不存库。"""
    if not req.message.strip() and not _current_image(req)[0]:
        raise errors.bad_request("message 不能为空")

    # 找不到角色卡不一定是错：只要前端带了 character_prompt / system_prompt
    # 就照常出话；三者都没有时 _resolve_character 才抛 404。
    character = _resolve_character(db, req)

    _note_knowledge_gap(req)
    messages = _build_messages(db, req, character)
    try:
        generation = await generate.one_shot(
            db, messages, immersive=req.immersive_mode, think=req.think
        )
    except upstream.UpstreamError as exc:
        # 429 必须原样回 429：老客户端把它当"服务繁忙"，退避时间比普通失败长得多；
        # 回 502 它会 700ms/1400ms 连试三次，越试越糟（老网关当年就是这么分的）。
        if exc.status == 429:
            raise errors.AppError(
                "rate_limit", "上游 AI 限速,请稍后重试", http_status=429
            ) from exc
        raise errors.AppError("upstream_error", f"上游没回：{exc}", http_status=502) from exc

    return LegacyChatResponse(
        reply=generation.text,
        mood="",
        usage={
            "prompt_tokens": generation.prompt_tokens,
            "completion_tokens": generation.completion_tokens,
            "cached_tokens": generation.cached_tokens,
            "total_ms": generation.total_ms,
        },
        knowledge_source="worldbook" if generation.text else "none",
    )


@router.get("/instances")
def legacy_instances(db: DbSession) -> dict[str, Any]:
    """老网关的实例清单（`GET /instances`）。

    新后端是**单进程**，没有 prompt/chat 那十几台实例。但运维脚本和排障面板会读这个
    路径，所以这里返回同样的骨架：每个模块一个"实例"，就是当前这个进程自己。

    形状与老网关一致（`modules.chat[]` / `modules.prompt[]` / `scaling`），
    额外多三个字段：`mode`（一看就知道是新后端）、`keys`（密钥池健康度）、`note`。
    老脚本忽略多余字段，不会因此报错。
    """
    settings = get_settings()
    alive = _db_alive(db)
    stamp = utcnow()
    last_seen = stamp.strftime("%Y-%m-%d %H:%M:%S")
    node = {
        "id": f"inline-{settings.port}",
        "host": settings.host,
        "port": settings.port,
        "status": "healthy" if alive else "down",
        "latency_ms": 0,
        "last_seen": last_seen,
        "last_seen_ts": last_seen,
        # 单进程没有"实例忙不忙"这回事；连接池的并发由 asyncio 自己管
        "busy": 0,
    }
    return {
        "modules": {"prompt": [dict(node)], "chat": [dict(node)]},
        "updated_at": last_seen,
        "scaling": {"prompt_count": 1, "chat_count": 1},
        "mode": settings.gen_mode,
        "keys": admin_repo.key_stats(db),
        "note": "新后端单进程（inline），没有多实例池；这两条是兼容形状，不是真实实例",
    }


@router.post("/instances/scale")
def legacy_scale() -> dict[str, Any]:
    """老的动态扩缩容（`POST /instances/scale`）。

    新后端没有实例可伸缩 —— 直接说清楚，而不是假装成功。
    返回 200 + `ok:false`：脚本不会崩，日志里能一眼看到"这条路已经没意义"。
    """
    return {
        "ok": False,
        "changed": [],
        "config": {"modules": {"prompt": [], "chat": []}, "scaling": {}},
        "note": "新后端单进程，没有实例可伸缩；要限流请调 key_rpm / user_rpm",
    }


@router.post("/chat/suggestions")
async def legacy_suggestions(req: LegacyChatRequest, db: DbSession) -> dict[str, Any]:
    """老契约的输入推荐（`/chat/suggestions`）。

    老客户端**点开推荐面板才拉**，不是每轮都拉 —— 这条规矩别改：
    每轮多一次上游调用，限速会提前撞满，表现就是"聊一会儿自己停了"。

    失败一律返回空列表（老服务就是这么写的）。推荐挂了不该让聊天界面弹错。
    """
    character = _find_character(db, req)
    lines: list[str] = []
    for item in req.history[-12:]:
        text = item.content.strip()
        if not text:
            continue
        who = "玩家" if item.role == "user" else "对方"
        lines.append(f"{who}：{text}")
    if req.message.strip():
        lines.append(f"玩家：{req.message.strip()}")

    try:
        items = await suggestions.suggest_from_pairs(
            db,
            character_name=character.name if character is not None else req.character,
            transcript="\n".join(lines),
        )
    except Exception as exc:  # noqa: BLE001 —— 推荐挂了绝不能影响聊天界面
        logger.warning("老契约推荐失败，按老服务返回空列表：%s", exc)
        return {"suggestions": []}
    return {"suggestions": items}


@router.post("/chat/stream")
async def legacy_chat_stream(
    req: LegacyChatRequest, db: DbSession, request: Request
) -> StreamingResponse:
    """老契约流式。帧名只有 `delta` / `done`（老客户端只认这两个）。"""
    if not req.message.strip() and not _current_image(req)[0]:
        raise errors.bad_request("message 不能为空")

    # 找不到角色卡不一定是错：只要前端带了 character_prompt / system_prompt
    # 就照常出话；三者都没有时 _resolve_character 才抛 404。
    character = _resolve_character(db, req)

    _note_knowledge_gap(req)
    messages = _build_messages(db, req, character)
    # 数据库的活到这里就完了；流开始后不再持有连接
    db.commit()

    async def frames() -> AsyncIterator[bytes]:
        parts: list[str] = []
        try:
            async for chunk in generate.stream_text(
                messages, immersive=req.immersive_mode, think=req.think
            ):
                parts.append(chunk)
                yield _sse("delta", {"text": chunk})
        except Exception as exc:  # noqa: BLE001 —— 流里什么都能抛
            logger.warning("老契约流式失败：%s", exc)
            # 限速单独标一个 code：客户端不读它，但排障时能一眼分清"上游限了"还是"上游挂了"
            code = "rate_limit" if getattr(exc, "status", None) == 429 else "upstream_error"
            yield _sse("error", {"code": code, "message": str(exc)[:200]})
            return
        yield _sse(
            "done",
            {
                "reply": "".join(parts),
                "mood": "",
                "usage": {},
                "knowledge_source": "worldbook" if parts else "none",
            },
        )

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _note_knowledge_gap(req: LegacyChatRequest) -> None:
    """老客户端要"联网搜索"时留一条痕。

    新后端**没有联网搜索**（检索只走自有世界书）。这里不报错、也不假装联网，
    只记一条日志 —— 攒够样本才知道要不要真做（见 docs/12）。
    """
    if req.force_search or req.use_rag is False:
        logger.info(
            "老客户端请求了联网搜索（force_search=%s use_rag=%s）：新后端不支持，已按世界书回答",
            req.force_search,
            req.use_rag,
        )


def _db_alive(db: DbSession) -> bool:
    """库通不通。`/instances` 的 status 用它 —— 宁可报 down，也不要假装健康。"""
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 —— 探活就是要接住一切
        return False


def _sse(event: str, data: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


__all__ = ["router"]
