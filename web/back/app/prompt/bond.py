"""prompt/bond.py —— 把干员记忆排成一段 system 文本。

为什么单独一个文件：这段文本的形状（篇幅、顺序、怎么写）是提示词问题，
不是存储问题。存储那边只管往库里写条目，这里只管"怎么端给模型看"。

三条规矩：
  1. **上限硬在 24 条 / 600 字**。账本可以有 40 条，但每轮都全塞会挤掉剧情。
  2. 顺序照库里的（置顶优先、最近更新优先），不做二次排序 —— 顺序就是"她最在意的"。
  3. 一行一条、以「·」开头，和永久记忆同一个视觉口径，模型更容易当事实读。
"""

from collections.abc import Sequence

BOND_MAX_ROWS = 24
BOND_MAX_CHARS = 600


def format_bond(rows: Sequence[object]) -> str:
    """`rows` 是一批带 `.text` 的记忆条目。返回可直接拼进 system 的正文。"""
    lines: list[str] = []
    used = 0
    for row in rows[:BOND_MAX_ROWS]:
        line = f"· {getattr(row, 'text', '')}".strip()
        if len(line) <= 2:
            continue
        if used + len(line) > BOND_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


__all__ = ["BOND_MAX_CHARS", "BOND_MAX_ROWS", "format_bond"]
