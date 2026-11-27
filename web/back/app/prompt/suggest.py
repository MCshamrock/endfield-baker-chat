"""prompt/suggest.py —— 输入推荐（"接下来我可以说什么"）的提示词与解析。

这个功能在三个地方要用：老客户端 `/chat/suggestions`、老群聊 `/group/suggestions`、
新客户端 `/v1/conversations/{id}/suggestions`。三处共用同一段系统提示词和同一个解析器，
否则同一个问题在三个入口会给出三种风格。

两条口径（都是从老后端抄的，别改）：
  1. **推荐的是"玩家会打出去的话"**，第一人称、短、口语，不是旁白也不是角色的台词。
  2. **失败一律返回空列表**。推荐是锦上添花，不能因为它挂掉就让聊天界面弹错。
"""

import json

# 老后端 group_chat_service.SUGGEST_SYSTEM 原文，只补了"身份"这一句
SUGGEST_SYSTEM = """你在帮一个角色扮演聊天的玩家想"接下来可以说什么"。

要求：
1. 输出 3 条候选，每条都是**玩家自己**会打出去的话（第一人称，直接可发送）。
2. 每条不超过 25 个字，短、口语化，像真的在打字。
3. 要贴合当前对话：承接最近的话题、回应对方、或者推进剧情。
4. 三条要有差异：一条顺着聊、一条换个角度、一条抛出新的引子。
5. 符合玩家此刻的身份与说话风格（见下方身份说明）。

严格只输出 JSON 数组，不要任何多余文字：
["候选1", "候选2", "候选3"]
"""

MAX_CHARS = 25
MAX_ITEMS = 3


def build_material(
    *,
    character_name: str = "",
    identity_note: str = "",
    topic: str = "",
    transcript: str = "",
    members: str = "",
) -> str:
    """拼给模型看的材料。空的部分自动省略，不要留一堆"（无）"。"""
    lines: list[str] = []
    if identity_note:
        lines.append(f"【玩家身份】{identity_note}")
    if members:
        lines.append(f"【在场的人】{members}")
    elif character_name:
        lines.append(f"【正在和谁说话】{character_name}")
    if topic:
        lines.append(f"【当前话题】{topic}")
    lines.append(f"【最近对话】\n{transcript or '（还没有对话）'}")
    lines.append("请给出玩家接下来可以说的 3 句话。")
    return "\n".join(lines)


def parse_suggestions(text: str, *, count: int = MAX_ITEMS) -> list[str]:
    """从模型输出里抠 JSON 数组。

    抠不出来就返回空列表 —— 让调用方走"没有推荐"这条路，而不是抛异常。
    这不是宽容，是这类辅助功能应有的失败姿势。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    cleaned: list[str] = []
    for item in items:
        value = str(item).strip().strip('"').strip("「」")
        if not value:
            continue
        # 太长就截断：推荐条目本来就该短，截到 25 字仍然可用
        cleaned.append(value[:MAX_CHARS])
        if len(cleaned) >= count:
            break
    return cleaned


__all__ = ["MAX_CHARS", "MAX_ITEMS", "SUGGEST_SYSTEM", "build_material", "parse_suggestions"]
