"""imports/normalize.py —— 把老包里的字段名翻译成后端自己的形状。

老数据包横跨很多版本，"同一个东西"在不同版本里叫不同名字，这个文件就是那张对照表：

    卡片     conversations | subs | children
    群成员   members | groupMembers | group_members | memberNames | memberList
             （也可能整组塞在 group / groupInfo / groupMeta / groupConfig 子对象里）
    会话     messages | items
    消息     text | content；side(mine/other) | role(user/assistant)
    顶层     cards | characterCards | characters；promptOverrides | prompts

两条原则：

1. **只翻译，不判断业务**。"这个角色收不收录""这张图要不要落盘"是 apply.py 的事，
   这里只回答"包里写的是什么"。
2. **认不出来的就丢，但记账**。空消息、坏图、认不出的图片格式，直接跳过并计数 ——
   导入一条畸形数据，会让整个会话在后面每次出话时都出问题，代价比少一条大得多。
"""

from dataclasses import dataclass, field

from app.imports import images
from app.imports.images import RawImage
from app.imports.package import PackageError, RawPackage

# 群聊发言模式的合法值（和 /v1 的 ConversationCreateRequest 保持一致）
# 注意包含 weighted：新版群聊的主路径（按 active 权重加权随机）。
# 以前这里只有三种，导入一份 weighted 的群聊会被悄悄降级成 smart，
# 玩家的"发言权重"设置就白设了。
SPEAK_MODES = ("round", "smart", "assign", "weighted")

# 群聊判定门槛：成员 ≥2 才算群聊（客户端同值）
GROUP_MIN_MEMBERS = 2


@dataclass(slots=True)
class RawMessage:
    """一条消息。`side` 已经是后端的三值（user/assistant/system）。"""

    side: str
    text: str
    speaker: str = ""
    mood: str = ""
    image: RawImage | None = None


@dataclass(slots=True)
class RawConversation:
    """一段对话 = 新后端里的一个会话。"""

    name: str
    messages: list[RawMessage] = field(default_factory=list)
    # 老包同时存了"屏上消息"和"AI 记得的历史"，这里被丢掉的历史条数（报告用）
    dropped_context: int = 0


@dataclass(slots=True)
class RawCard:
    """一张主卡。单聊卡是没有 members 的普通卡，群聊卡带 members。"""

    conversations: list[RawConversation] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    my_role: str = ""
    speak_mode: str = "smart"

    @property
    def is_group(self) -> bool:
        return len(self.members) >= GROUP_MIN_MEMBERS


@dataclass(slots=True)
class RawProject:
    """整个包归一化后的结果。apply.py 只认这个结构，不再看原始字典。"""

    version: int
    cards: list[RawCard]
    prompts: dict[str, str]
    order: list[str]
    my_gender: str
    # 各类"丢了多少"的计数，汇总进导入报告
    dropped_messages: int = 0
    dropped_images: int = 0
    dropped_context: int = 0


def normalize(pkg: RawPackage) -> RawProject:
    """原始字典 → 归一化结构。一个有效对话都没有就报错（而不是导一个空包）。"""
    project = RawProject(
        version=pkg.version,
        cards=[],
        prompts=dict(pkg.prompts),
        order=list(pkg.order),
        my_gender=_gender_of(pkg.root),
    )
    for raw_card in pkg.cards:
        card = _card(raw_card, pkg, project)
        if card is not None:
            project.cards.append(card)

    if not project.cards:
        raise PackageError("包里没有可导入的对话（消息都是空的，或者全是错误气泡）")
    return project


# ---------------------------------------------------------------- 卡片
def _card(raw: dict[str, object], pkg: RawPackage, project: RawProject) -> RawCard | None:
    """一张卡里所有子对话都空 → 整张卡丢掉（客户端导出时也这么过滤）。"""
    conversations = [
        conversation
        for conversation in (
            _conversation(item, pkg, project) for item in _conversations_of(raw)
        )
        if conversation is not None
    ]
    if not conversations:
        return None

    members, my_role, speak_mode = _group_meta(raw)
    return RawCard(
        conversations=conversations,
        members=members,
        my_role=my_role,
        speak_mode=speak_mode,
    )


def _conversations_of(card: dict[str, object]) -> list[dict[str, object]]:
    """子对话数组的三个历史名字。"""
    for key in ("conversations", "subs", "children"):
        value = card.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _group_meta(card: dict[str, object]) -> tuple[list[str], str, str]:
    """读群聊元数据。显式字段优先，字段缺了就当单聊（这里不猜）。"""
    scope = _group_scope(card)
    members = _pick_members(scope)
    my_role = _first_str(scope, ("myRole", "role"))
    speak_mode = _first_str(scope, ("speakMode", "mode")).lower()
    if speak_mode not in SPEAK_MODES:
        speak_mode = "smart"
    return members, my_role, speak_mode


def _group_scope(card: dict[str, object]) -> dict[str, object]:
    """把群聊字段所在的作用域展平：可能是平铺的，也可能塞在 group 子对象里。"""
    for key in ("group", "groupInfo", "groupMeta", "groupConfig"):
        nested = card.get(key)
        if isinstance(nested, dict):
            merged = dict(card)
            merged.update(nested)
            return merged
    return card


def _pick_members(scope: dict[str, object]) -> list[str]:
    """成员名单：去重、去空、保序。一个都没有就是空数组（= 单聊卡）。"""
    for key in ("members", "groupMembers", "group_members", "memberNames", "memberList"):
        value = scope.get(key)
        if not isinstance(value, list):
            continue
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in names:
                names.append(item.strip())
        if names:
            return names
    return []


# ---------------------------------------------------------------- 对话
def _conversation(
    raw: dict[str, object], pkg: RawPackage, project: RawProject
) -> RawConversation | None:
    """一段对话。名字优先用 title（新版字段），没名字就丢掉（上层没法显示它）。"""
    name = _first_str(raw, ("name", "title"))
    if not name:
        return None

    messages = [
        message
        for message in (_message(item, pkg, project) for item in _messages_of(raw))
        if message is not None
    ]
    conversation = RawConversation(name=name, messages=messages)

    # 屏上消息被清空过、但 AI 上下文还在 → 用上下文兜底，否则这段记忆就彻底没了
    context = _context_of(raw, pkg)
    if not messages and context:
        conversation.messages = [
            RawMessage(side=entry[0], text=entry[1], image=entry[2]) for entry in context
        ]
    elif context:
        # 两者都有且不一致时以屏上消息为准：AI 的记忆可以从消息重建，
        # 反过来重建不了（消息才是用户认为的"我的记录"）。丢掉的条数记进报告。
        project.dropped_context += len(context)

    return conversation if conversation.messages else None


def _messages_of(conversation: dict[str, object]) -> list[dict[str, object]]:
    for key in ("messages", "items"):
        value = conversation.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _context_of(
    conversation: dict[str, object], pkg: RawPackage
) -> list[tuple[str, str, RawImage | None]]:
    """AI 上下文历史（`contextHistory`），只在"没有可见消息"时用来兜底。"""
    raw = conversation.get("contextHistory")
    if not isinstance(raw, list):
        return []
    entries: list[tuple[str, str, RawImage | None]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = _first_str(item, ("text", "content"))
        if not text:
            continue
        entries.append((_side_of(item), text, _image_of(item.get("image"), pkg)))
    return entries


# ---------------------------------------------------------------- 消息
def _message(raw: dict[str, object], pkg: RawPackage, project: RawProject) -> RawMessage | None:
    """一条消息。错误气泡、空消息直接跳过并记账。"""
    if raw.get("isError"):
        project.dropped_messages += 1
        return None

    image = _image_of(raw.get("image"), pkg)
    if raw.get("image") and image is None:
        project.dropped_images += 1

    text = _first_str(raw, ("text", "content"))
    if not text and image is None:
        project.dropped_messages += 1
        return None

    # 「新话题」提示行：屏上要显示，但不进 AI 上下文 → system 侧
    side = "system" if raw.get("topic") else _side_of(raw)
    return RawMessage(
        side=side,
        text=text,
        speaker=_first_str(raw, ("speakerName", "speaker")),
        mood=_first_str(raw, ("mood",)),
        image=image,
    )


def _side_of(raw: dict[str, object]) -> str:
    """方向 → 后端的 side。role 是更老的字段名，优先认它（新版两者都在且一致）。"""
    role = raw.get("role")
    if role in ("user", "assistant", "system"):
        return str(role)
    side = raw.get("side")
    if side == "mine":
        return "user"
    # other / 缺失：老客户端默认按对方算
    return "assistant"


def _image_of(ref: object, pkg: RawPackage) -> RawImage | None:
    """图片引用 → 字节。ZIP 包走 `pkg.read_member` 读包内文件，JSON 包解 dataURL。"""
    if not isinstance(ref, str):
        return None
    return images.decode(ref, pkg.read_member)


# ---------------------------------------------------------------- 小工具
def _first_str(raw: dict[str, object], keys: tuple[str, ...]) -> str:
    """按顺序取第一个非空字符串。老字段名就是这么一个个试出来的。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _gender_of(root: dict[str, object]) -> str:
    """玩家性别：新版 myGender，旧版 gender / isFemale。只存进报告，不影响会话。"""
    if root.get("isFemale") is True:
        return "female"
    value = root.get("myGender") or root.get("gender")
    if value in ("female", "f", 1):
        return "female"
    return "male"


__all__ = [
    "GROUP_MIN_MEMBERS",
    "RawCard",
    "RawConversation",
    "RawMessage",
    "RawProject",
    "normalize",
]
