"""tests/test_budget.py —— 上下文预算：只算额度、只切"要压哪一段"，不裁切任何内容。

这一组守的是一个很现实的失败模式：会话聊到一百多轮，
如果每轮都把历史全塞进去，上游会从 150 token/s 掉到 30 —— 用户体感是"越聊越慢"。
但降速的解法是**压缩**，不是丢消息：所以这里没有任何"扔掉"的函数。
"""

from app.prompt.budget import (
    estimate_tokens,
    history_budget,
    messages_tokens,
    take_oldest,
)


def test_estimate_grows_with_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") < estimate_tokens("你好" * 10)


def test_take_oldest_respects_cap_and_keeps_tail() -> None:
    history = [{"role": "user", "content": "第" * 200} for _ in range(10)]
    # 每条约 134 token；预算 300 只够 2 条；最近 3 条不许动
    chunk, rest = take_oldest(history, max_tokens=300, keep_tail=3)
    assert len(chunk) == 2
    assert rest == history[2:]
    assert rest[-3:] == history[-3:]


def test_take_oldest_always_takes_at_least_one() -> None:
    """单条就超过上限时也要切 —— 否则这条永远压不掉，预算永远降不下来。"""
    huge = [{"role": "user", "content": "字" * 10_000}]
    chunk, rest = take_oldest(huge, max_tokens=10, keep_tail=0)
    assert len(chunk) == 1
    assert rest == []


def test_take_oldest_returns_empty_when_only_tail_left() -> None:
    history = [{"role": "user", "content": "a"} for _ in range(2)]
    chunk, rest = take_oldest(history, max_tokens=1000, keep_tail=2)
    assert chunk == []
    assert rest == history


def test_take_oldest_does_not_mutate_input() -> None:
    history = [{"role": "user", "content": "a"} for _ in range(5)]
    before = list(history)
    take_oldest(history, max_tokens=1, keep_tail=0)
    assert history == before


def test_history_budget_subtracts_prefix_and_reply_reserve() -> None:
    # 1000 − 105 − 100 = 795
    assert history_budget(1000, prefix_tokens=105, reply_reserve=100) == 795


def test_history_budget_never_negative() -> None:
    assert history_budget(100, prefix_tokens=200, reply_reserve=100) == 0


def test_messages_tokens_counts_structural_overhead() -> None:
    single = [{"role": "user", "content": ""}]
    assert messages_tokens(single) == 4  # 空内容 + 每条 4 的结构开销
