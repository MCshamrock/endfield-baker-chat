"""retrieval/score.py —— 三档打分（exact / fuzzy / semantic），移植自内核 480–528。

三档必须能说清差别，不许糊成一档：

  * `exact`   原词/别称**直接命中**（子串）。最准，但用户换个说法就漏。
              每个命中 `10 + min(词长, 8)`；标题被整串包含再 +30。
  * `fuzzy`   错别字、谐音、少写一个字也能命中（编辑距离）。
              词长 ≤4 允许差 1（得 8 分），>4 允许差 2（得 6 分），每多差一个字再扣 2。
  * `semantic` 整句自然语言 → 与「标题 + keys + semantic」的字符二元组相似度。
              治"用户问的是概念，不是词条名"。
              两把尺子取高：dice（怕短查询被长条目稀释）与 coverage（短查询更公平）。
              **故意不设硬阈值** —— 这档返回的是"候选菜单"，挑哪条由模型看着 semantic 定；
              设了阈值就会出现"换个说法一条都搜不到"，比给三条候选更糟。
"""

from typing import TypedDict

from app.retrieval.text import bigrams, edit_distance, norm_text, split_tokens

PRIORITY_RANK = {"high": 3, "mid": 2, "low": 1}


class Hit(TypedDict):
    """一条命中结果。字段名跟内核保持一致（模型/测试都按这个读）。"""

    id: str
    title: str
    semantic: str
    keys: list[str]
    kind: str
    priority: str
    score: float
    hitKeys: list[str]


def score_entry(
    *,
    entry_id: str,
    title: str,
    keys: list[str],
    fuzzy: list[str],
    semantic: str,
    kind: str,
    priority: str,
    query: str,
    mode: str,
) -> Hit | None:
    """给一条条目打分。不命中返回 None（调用方据此跳过）。

    三档各是一个小函数，主函数只做分发 —— 这样每档的公式能单独读、单独测。
    """
    q = norm_text(query)
    if not q:
        return None

    if mode == "exact":
        score, hit_keys = _score_exact(q, title, keys, fuzzy)
    elif mode == "fuzzy":
        score, hit_keys = _score_fuzzy(q, keys, fuzzy)
    else:
        score, hit_keys = _score_semantic(q, title, keys, semantic)

    if not score:
        return None

    return {
        "id": entry_id,
        "title": title,
        "semantic": semantic,
        "keys": keys,
        "kind": kind or "term",
        "priority": priority or "mid",
        "score": round(score, 2),
        "hitKeys": hit_keys,
    }


def _score_exact(
    query: str, title: str, keys: list[str], fuzzy: list[str]
) -> tuple[float, list[str]]:
    """精确档：关键词子串命中，每个 `10 + min(词长, 8)`；标题整串命中再 +30。"""
    score = 0.0
    hit_keys: list[str] = []
    for key in [*keys, *fuzzy]:
        kk = norm_text(key)
        if len(kk) >= 2 and kk in query:
            score += 10 + min(len(kk), 8)
            hit_keys.append(key)
    if title and norm_text(title) in query:
        score += 30
    return score, hit_keys


def _score_fuzzy(query: str, keys: list[str], fuzzy: list[str]) -> tuple[float, list[str]]:
    """模糊档：查询切词后与关键词比编辑距离（短词容差 1、长词容差 2）。"""
    score = 0.0
    hit_keys: list[str] = []
    tokens = split_tokens(query)
    for key in [*keys, *fuzzy]:
        kk = norm_text(key)
        if len(kk) < 2:
            continue
        limit = 1 if len(kk) <= 4 else 2
        for token in tokens:
            if abs(len(token) - len(kk)) > limit:
                continue
            distance = edit_distance(token, kk, limit)
            if distance <= limit:
                score += (8 if limit == 1 else 6) - distance * 2
                hit_keys.append(key)
                break
    return score, hit_keys


def _score_semantic(
    query: str, title: str, keys: list[str], semantic: str
) -> tuple[float, list[str]]:
    """语义档：字符二元组的 dice 与 coverage 取高，关键词子串再少量加分。

    故意不设阈值 —— 这档给的是"候选菜单"，不是"命中/不命中"。
    """
    bag = f"{title} {''.join(keys)} {semantic}"
    query_pairs = bigrams(query)
    bag_pairs = bigrams(bag)
    overlap = len(query_pairs & bag_pairs)
    dice = (2 * overlap) / (len(query_pairs) + len(bag_pairs)) if query_pairs and bag_pairs else 0.0
    coverage = overlap / len(query_pairs) if query_pairs else 0.0

    bonus = 0.0
    hit_keys: list[str] = []
    for key in keys:
        kk = norm_text(key)
        if len(kk) >= 2 and kk in query:
            bonus += 4
            hit_keys.append(key)

    return max(dice * 20, coverage * 14) + bonus, hit_keys


def sort_hits(hits: list[Hit]) -> list[Hit]:
    """排序：分数降序 → 优先级 → 标题（保证三端结果完全一致）。

    `title` 的比较用 Unicode 码位，不用 locale —— locale 排序在不同机器上不一样，
    那会让"同一句话在不同设备上搜出不同顺序"。
    """
    return sorted(
        hits,
        key=lambda hit: (
            -hit["score"],
            -PRIORITY_RANK.get(str(hit["priority"]), 0),
            str(hit["title"]),
        ),
    )


__all__ = ["PRIORITY_RANK", "score_entry", "sort_hits"]
