"""retrieval/text.py —— 检索用的四个纯函数，逐行移植自聊天终端内核。

来源：`F:\\dshworkspace\\programs\\ai_chat_terminal\\tavern_core.js`
  normText       443
  bigrams        444
  editDistance   452
  normalizeMode  470

为什么照抄而不"优化"：
  这套算法是两端（Windows / 安卓）跑了很久、并且有离线断言（test_tavern_core.js）的。
  重写一版"更优雅的"等于把那些断言全部作废，还会让三个端的检索结果不一致。
  移植的验收标准是：同样的输入，Python 和 JS 给出同样的分数。

唯一不能照抄的地方：JS 的 `Set` 在 Python 里用 `set`，语义一致。
"""

import re

# 只保留中日韩统一表意文字、英文、数字 —— 标点、空格、emoji 全部丢掉
_KEEP_PATTERN = re.compile(r"[^\u4e00-\u9fa5a-z0-9]")

# 模糊档切词用的分隔符（和 JS 里那个正则一一对应）
_TOKEN_SPLIT = re.compile(r"[\s,，。、;；:：!！?？\"'「」（）()\[\]【】]+")

MODE_ALIAS = {
    "exact": "exact",
    "precise": "exact",
    "精确": "exact",
    "keyword": "exact",
    "fuzzy": "fuzzy",
    "模糊": "fuzzy",
    "typo": "fuzzy",
    "semantic": "semantic",
    "语义": "semantic",
    "meaning": "semantic",
}


def norm_text(value: object) -> str:
    """小写 + 空白折叠 + 去首尾。"""
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text).lower().strip()


def bigrams(value: str) -> set[str]:
    """字符二元组（语义档用）。单个字符时退化成它自己。"""
    text = _KEEP_PATTERN.sub("", norm_text(value))
    pairs: set[str] = set()
    for index in range(len(text) - 1):
        pairs.add(text[index : index + 2])
    if len(text) == 1:
        pairs.add(text)
    return pairs


def edit_distance(a: str, b: str, limit: int) -> int:
    """带上限的编辑距离：超过 limit 直接返回 limit+1（省算力）。"""
    s, t = str(a), str(b)
    if abs(len(s) - len(t)) > limit:
        return limit + 1

    prev = list(range(len(t) + 1))
    cur = [0] * (len(t) + 1)
    for i in range(1, len(s) + 1):
        cur[0] = i
        best = cur[0]
        for j in range(1, len(t) + 1):
            cost = 0 if s[i - 1] == t[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < best:
                best = cur[j]
        if best > limit:
            return limit + 1
        prev, cur = cur, prev
    return prev[len(t)]


def normalize_mode(mode: object) -> str:
    """模式别名归一：不认识的输入一律当 exact（最保守）。"""
    key = str(mode or "").strip().lower()
    return MODE_ALIAS.get(key, "exact")


def split_tokens(query: str) -> list[str]:
    """模糊档的切词：按标点/空格切开，丢掉单字，最多 12 个。"""
    return [part for part in _TOKEN_SPLIT.split(query) if len(part) >= 2][:12]


__all__ = [
    "MODE_ALIAS",
    "bigrams",
    "edit_distance",
    "norm_text",
    "normalize_mode",
    "split_tokens",
]
