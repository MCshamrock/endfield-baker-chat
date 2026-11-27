"""models/characters.py —— 角色与提示词。

角色的"是什么"（名字、头像、属性）和"怎么说话"（提示词）分两张表，原因是变动频率差很多：
名字头像基本不动，提示词会反复调版本。分开之后改人设只是多插入一行，历史版本还能回退。
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, LongText, SmallFlag, UInt64, UtcDateTime


class Character(Base):
    """角色。服务端权威，客户端只缓存（客户端不许再硬编码角色表）。"""

    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("slug", name="uk_characters_slug"),
        Index("ix_characters_enabled", "enabled", "sort_order"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    # slug 是稳定英文 id（perlica），客户端拿它建会话；中文名只用来显示
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    attribute: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)
    enabled: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=1)
    # 群聊发言权重（1~100，越大越容易开口）。默认值不是全员一样 —— 等权等于没有权重
    active_weight: Mapped[int] = mapped_column(nullable=False, default=50)


class CharacterPrompt(Base):
    """角色提示词，按「变体 + 版本」存。改人设不覆盖历史，写坏了能回退。

    两个容易混的概念：

    * `variant` = 哪一套提示词。'v1' 是老包的常规长卡（默认），
      'v2' 是老客户端打开 `use_new_prompt` 时才用的实验性长卡。
      同一个角色可以同时有 v1 和 v2 两行，各自都是一套完整的正文。
    * `ver` = 同一套里的第几版。改人设就 +1，别原地覆盖。

    所以唯一键是 (character_id, variant, ver)：v1 和 v2 都能有 ver=1，不会撞。
    """

    __tablename__ = "character_prompts"
    __table_args__ = (
        UniqueConstraint("character_id", "variant", "ver", name="uk_prompts_char_variant_ver"),
        Index("ix_prompts_active", "character_id", "variant", "active"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("characters.id", name="fk_prompts_char"), nullable=False
    )
    ver: Mapped[int] = mapped_column(nullable=False)
    # 提示词变体：v1 = 老包常规长卡，v2 = 实验性长卡。给已有的行自动填 v1（迁移里也是这么做的）
    variant: Mapped[str] = mapped_column(String(8), nullable=False, default="v1")
    body: Mapped[str] = mapped_column(LongText, nullable=False)
    active: Mapped[int] = mapped_column(SmallFlag, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DevicePrompt(Base):
    """这台设备自己改过的角色提示词。

    和 `CharacterPrompt` 的区别：那张表是全局的（改了对所有人生效），
    这张表是私人的（只影响一台设备的一个角色）。导入老数据包时，
    用户自己写过的提示词落在这里 —— 不丢，也不污染别人。
    """

    __tablename__ = "device_prompts"
    __table_args__ = (
        UniqueConstraint("device_id", "character_id", name="uk_dprompt_device_char"),
    )

    id: Mapped[int] = mapped_column(UInt64, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("devices.id", name="fk_dprompt_device"), nullable=False
    )
    character_id: Mapped[int] = mapped_column(
        UInt64, ForeignKey("characters.id", name="fk_dprompt_char"), nullable=False
    )
    body: Mapped[str] = mapped_column(LongText, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


__all__ = ["Character", "CharacterPrompt", "DevicePrompt"]
