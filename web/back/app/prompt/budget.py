"""prompt/budget.py —— 上下文预算：**超了就压缩，绝不裁切**。

这条是硬规矩，理由只有一条：裁切会丢记忆，且丢得悄无声息。
用户聊了两百轮，回头发现"她怎么不记得我上周说的话"——那不是模型笨，是我们把原文扔了。

所以这里的职责只有两个：

1. 算「这一轮到底能给历史留多少额度」（总预算 − 固定前缀 − 回复余量）；
2. 从最老的一端**切出一段交给压缩器**（切的是"要压哪一段"，不是"丢掉哪一段"）。

关于 token 估算：这里不用分词器，按"1.5 个字符 ≈ 1 token"估。
中文按字算、英文按字符算，这个系数偏保守（宁多算不少算）。
真要说准，只有上游返回的 `usage.prompt_tokens` 是准的 —— 那是事后才知道的。
"""

from app.prompt.dialog import Message

# 中文/混合文本的经验系数：约 1.5 个字符一个 token
_CHARS_PER_TOKEN = 1.5


def estimate_tokens(text: str) -> int:
    """粗估 token 数。空串算 0。"""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def messages_tokens(messages: list[Message]) -> int:
    """估一组消息的总 token（每条再加 4 个结构开销，跟上游的算法接近）。"""
    return sum(estimate_tokens(str(item.get("content", ""))) + 4 for item in messages)


def history_budget(prompt_budget_tokens: int, prefix_tokens: int, reply_reserve: int) -> int:
    """历史能用的额度 = 总预算 − 固定前缀 − 给回复留的余量。

    固定前缀是"不压的那几段 system"（协议/世界观/索引/常驻/角色/提要/永久记忆/检索）。
    回复余量是怕历史刚好顶满、模型没地方说话。
    """
    return max(0, prompt_budget_tokens - prefix_tokens - reply_reserve)


def take_oldest(
    messages: list[Message], max_tokens: int, keep_tail: int = 0
) -> tuple[list[Message], list[Message]]:
    """从最老的一端切出一段交给压缩器。返回 (要压的那段, 剩下的)。

    两个边界：
      * `keep_tail`：最近几条必须留在原文里。摘要再准也会丢语气，刚说完的话要逐字在。
      * **至少切一条**：哪怕单条消息本身就超过 `max_tokens`，也要切它 ——
        否则这条永远压不掉，预算永远降不下来。切片只影响"这一次摘要调用的输入多大"，
        不影响记忆完整性（内容会进摘要）。
    """
    limit = max(0, len(messages) - keep_tail)
    if limit == 0:
        return [], list(messages)

    taken: list[Message] = []
    used = 0
    for item in messages[:limit]:
        cost = estimate_tokens(str(item.get("content", ""))) + 4
        if taken and used + cost > max_tokens:
            break
        taken.append(item)
        used += cost
    return taken, messages[len(taken) :]


__all__ = ["estimate_tokens", "history_budget", "messages_tokens", "take_oldest"]
