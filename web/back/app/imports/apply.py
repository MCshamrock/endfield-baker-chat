"""imports/apply.py —— 把归一化后的包写进数据库。

到这里才碰数据库，前面两层（package / normalize）都是纯函数。顺序上做了三个决定，
每个决定都在下面代码里有注释，这里先讲清楚为什么：

1. **一个子对话 = 一个会话**。老客户端是"一张主卡下挂多段对话"（像存档），
   新后端是平铺的会话列表。一段存档建一条会话，用户点进去看到的就是那段对话，
   不会把三段不同时间线的对话糊成一条。

2. **角色靠名字认**。老包里只有中文名，用名字去 `characters` 表换 slug。
   认不出来的（用户自己改过名、或者新角色下架了）**照样建会话**，只是没有
   `character_id` —— 历史能看、能接着当普通对话聊，只是没有那个人的人设。
   丢历史比丢人设严重得多。

3. **消息时间就是导入时间**。老包里的消息没有时间戳（只有数组顺序），
   所以创建时间只能是"数据进来的这一刻"。顺序完全靠 seq 保证，
   前端要区分"这是迁移进来的老数据"就看 `origin='legacy'`。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.clock import utcnow
from app.imports.normalize import (
    GROUP_MIN_MEMBERS,
    RawCard,
    RawConversation,
    RawMessage,
    RawProject,
)
from app.models import Character, Conversation, Device
from app.repos import attachments as attachments_repo
from app.repos import characters as characters_repo
from app.repos import content as content_repo
from app.repos import conversations as conversations_repo
from app.repos import messages as messages_repo

# 会话标题的字段宽度（conversations.title 是 VARCHAR(120)）
TITLE_MAX = 120


@dataclass(slots=True)
class ImportResult:
    """导入结果。既是接口的返回，也是批次表里 `report` 的内容来源。"""

    cards: int = 0
    conversations: int = 0
    messages: int = 0
    images: int = 0
    device_prompts: int = 0
    # 丢掉的空消息 / 认不出的图片（在 normalize 阶段就算好了，这里累加）
    skipped: int = 0
    dropped_context: int = 0
    unknown_characters: list[str] = field(default_factory=list)
    unknown_prompt_owners: list[str] = field(default_factory=list)

    def note_unknown_character(self, name: str) -> None:
        """记一个"包里有、库里没有"的角色名（去重，保持出现顺序）。"""
        if name and name not in self.unknown_characters:
            self.unknown_characters.append(name)

    def counts(self) -> dict[str, int]:
        """写进批次表的那几个数字。"""
        return {
            "cards": self.cards,
            "conversations": self.conversations,
            "messages": self.messages,
            "images": self.images,
            "skipped": self.skipped,
        }

    def notes(self) -> list[str]:
        """给人看的中文说明。空列表 = 一个数据都没丢。"""
        messages: list[str] = []
        if self.unknown_characters:
            names = "、".join(self.unknown_characters[:10])
            more = "等" if len(self.unknown_characters) > 10 else ""
            messages.append(
                f"{len(self.unknown_characters)} 个角色新后端没有收录（{names}{more}）："
                "历史消息都导进来了，可以当普通对话接着聊，但没有它们的人设"
            )
        if self.unknown_prompt_owners:
            names = "、".join(self.unknown_prompt_owners[:10])
            messages.append(f"自定义提示词里有 {len(self.unknown_prompt_owners)} 个角色没收录（{names}），未写入")
        if self.dropped_context:
            messages.append(
                f"{self.dropped_context} 条 AI 上下文历史没有迁移：老包里的屏上消息与 AI 记忆不一致时，"
                "以屏上消息为准"
            )
        if self.skipped:
            messages.append(f"跳过 {self.skipped} 条空消息或无法识别的图片")
        return messages

    def report(self) -> dict[str, object]:
        return {
            "counts": self.counts(),
            "device_prompts": self.device_prompts,
            "dropped_context": self.dropped_context,
            "unknown_characters": self.unknown_characters,
            "unknown_prompt_owners": self.unknown_prompt_owners,
            "notes": self.notes(),
        }


@dataclass(slots=True)
class _Context:
    """一次导入过程中反复要用的东西。打包成一个对象，省得每个函数都传七个参数。"""

    db: Session
    device: Device
    batch_id: int
    upload_dir: str
    characters: dict[str, Character]
    result: ImportResult
    # 登录用户导入的会话直接归到账号名下（否则换台设备就看不见这批记录）
    user_id: int | None = None


def apply(
    db: Session,
    *,
    device: Device,
    project: RawProject,
    batch_id: int,
    upload_dir: str,
    user_id: int | None = None,
) -> ImportResult:
    """把整个包写进库。**不做 commit**，由调用方决定提交还是回滚。

    这样"导到一半炸了"就是干净的回滚，不会留下一半会话 —— 那比一条都没进去更糟，
    因为用户会以为导成功了。
    """
    result = ImportResult(
        skipped=project.dropped_messages + project.dropped_images,
        dropped_context=project.dropped_context,
    )
    context = _Context(
        db=db,
        device=device,
        batch_id=batch_id,
        upload_dir=upload_dir,
        characters={item.name: item for item in characters_repo.list_enabled(db)},
        result=result,
        user_id=user_id,
    )

    now = utcnow()
    for index, card in enumerate(project.cards):
        # 老包的 cards 数组就是"最近活跃在前"（v7 起显式导出 order）。
        # 每张卡往前挪一秒，会话列表照老顺序排；否则全部挤在同一微秒、靠 id 排。
        active_at = now - timedelta(seconds=index)
        _apply_card(context, card=card, active_at=active_at)
        result.cards += 1

    _apply_prompts(context, prompts=project.prompts)
    return result


def _apply_card(context: _Context, *, card: RawCard, active_at: datetime) -> None:
    """一张卡 → 若干个会话。群聊成员收不全时降级成单聊（历史不能丢）。"""
    members = [context.characters[name] for name in card.members if name in context.characters]
    for name in card.members:
        if name not in context.characters:
            context.result.note_unknown_character(name)

    if card.is_group and len(members) < GROUP_MIN_MEMBERS:
        # 两个成员只认出一个：硬凑成"群聊"会让人物关系错乱，不如按单聊导
        members = []

    for conversation in card.conversations:
        _apply_conversation(
            context, card=card, conversation=conversation, members=members, active_at=active_at
        )


def _apply_conversation(
    context: _Context,
    *,
    card: RawCard,
    conversation: RawConversation,
    members: list[Character],
    active_at: datetime,
) -> None:
    """一段子对话 → 一个会话（带上它的全部消息）。"""
    if members:
        character = None
        title = _group_title(members)
    else:
        character = context.characters.get(conversation.name)
        if character is None:
            context.result.note_unknown_character(conversation.name)
        title = conversation.name

    row = conversations_repo.create(
        context.db,
        device_id=context.device.id,
        user_id=context.user_id,
        character_id=character.id if character is not None else None,
        mode="group" if members else "single",
        title=title[:TITLE_MAX],
        last_active_at=active_at,
        import_batch_id=context.batch_id,
    )
    if members:
        row.speak_mode = card.speak_mode
        # 老包的 myRole 是 admin / observer / 某个角色名，新后端这一列就是自由文本
        row.player_role = (card.my_role or "admin")[:64]
        conversations_repo.create_members(
            context.db, conversation_id=row.id, character_ids=[item.id for item in members]
        )

    context.result.conversations += 1
    _apply_messages(context, row, conversation=conversation, character=character)


def _apply_messages(
    context: _Context,
    row: Conversation,
    *,
    conversation: RawConversation,
    character: Character | None,
) -> None:
    """按顺序落消息。`client_msg_id` 用 `i<批次>-<序号>`，同一会话内不会撞。"""
    for index, message in enumerate(conversation.messages):
        attachment_id = _save_image(context, message)
        messages_repo.insert(
            context.db,
            conversation=row,
            client_msg_id=f"i{context.batch_id}-{index + 1}",
            side=message.side,
            text=message.text,
            speaker=message.speaker or _default_speaker(message, character),
            attachment_id=attachment_id,
            origin="legacy",
            mood=message.mood,
        )
        context.result.messages += 1


def _save_image(context: _Context, message: RawMessage) -> int | None:
    """图片 → 附件表 + 落盘。同一张图重复出现时按内容复用（附件层已去重）。"""
    if message.image is None:
        return None
    attachment, _created = attachments_repo.save(
        context.db,
        upload_dir=context.upload_dir,
        data=message.image.data,
        mime=message.image.mime,
        device_id=context.device.id,
    )
    context.result.images += 1
    return attachment.id


def _apply_prompts(context: _Context, *, prompts: dict[str, str]) -> None:
    """用户自己改过的角色提示词 → `device_prompts`（只影响这台设备）。"""
    for name, body in prompts.items():
        character = context.characters.get(name)
        if character is None:
            context.result.unknown_prompt_owners.append(name)
            continue
        content_repo.upsert_device_prompt(
            context.db, device_id=context.device.id, character_id=character.id, body=body
        )
        context.result.device_prompts += 1


def _default_speaker(message: RawMessage, character: Character | None) -> str:
    """角色消息没写说话人时，单聊就用角色名（群聊必须靠 speaker 分人，不能瞎填）。"""
    if message.side == "assistant" and character is not None:
        return character.name
    return ""


def _group_title(members: list[Character]) -> str:
    """群聊标题：成员名用顿号连起来。老客户端的"…的群聊"是显示层拼的，不存。"""
    return "、".join(item.name for item in members)


__all__ = ["ImportResult", "apply"]
