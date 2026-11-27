"""models/__init__.py —— 把全部表的模型集中导出。

调用方只写 `from app.models import Conversation, Message`，不用关心它们分在哪个文件。
分文件的理由只有一个：一个文件塞十几张表会到 300 行以上，翻起来累。

注意：`Base` 和所有模型必须在这里被 import 过，否则 SQLAlchemy 的 metadata 里
不会有这张表 —— 测试里的"模型 vs 数据库"比对就会漏表。
"""

from app.models.base import Base
from app.models.characters import Character, CharacterPrompt, DevicePrompt
from app.models.chat import (
    Attachment,
    Conversation,
    ConversationMember,
    Message,
    Summary,
)
from app.models.identity import Device, User
from app.models.imports import ImportBatch
from app.models.memory import CharacterMemory, CharacterWeight, MemorySettings
from app.models.ops import ApiKey, ConfigEntry, Job, UsageEvent
from app.models.uploads import UploadTicket
from app.models.worldbook import WorldbookEntry

__all__ = [
    "ApiKey",
    "Attachment",
    "Base",
    "Character",
    "CharacterMemory",
    "CharacterPrompt",
    "CharacterWeight",
    "ConfigEntry",
    "Conversation",
    "ConversationMember",
    "Device",
    "DevicePrompt",
    "ImportBatch",
    "Job",
    "MemorySettings",
    "Message",
    "Summary",
    "UploadTicket",
    "UsageEvent",
    "User",
    "WorldbookEntry",
]
