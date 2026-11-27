"""scripts/eval_rag_corpus.py —— 给语料库做一次离线体检（换语料前后都该跑）。

两件事：

  1. **标题问法**：拿每条"设定条目"（kind=term）的标题造三句人话
     （「X是什么」「给我讲讲X」「X的设定」），看第一名是不是它自己。
     这项不需要额外数据，生产机上也能跑，测的是"用户直接点名"这种最常见的情况。
  2. **语料自带问法**：老库的 `corpus_v2.jsonl` 里每条语料都挂着 2~3 句合成问法，
     那是训练向量模型时造的，这里借来当评测集（`--queries` 指过去）。
     测的是"用户换着说法问"时的召回。

判定口径跟线上一致：命中 = 该条目所属文件进了前 12 候选（模型拿到的是前 12 段）。

用法：
    python scripts/eval_rag_corpus.py
    python scripts/eval_rag_corpus.py --queries /path/corpus_v2.jsonl --limit 0
"""

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.retrieval.corpus import (  # noqa: E402
    DEFAULT_MAX_DF_RATIO,
    DEFAULT_MAX_KEYS,
    build_entries,
    doc_path,
    doc_title,
    load_rows,
)
from app.retrieval.score import PRIORITY_RANK  # noqa: E402

DEFAULT_DOCS = Path(__file__).resolve().parent.parent / "data" / "rag_corpus" / "docs.jsonl"

# 标题命中的加分要和 app/retrieval/store.py 里保持一致
TITLE_BONUS = 30.0


class Matcher:
    """评测用的加速版 match_entries（语义相同，只多了倒排表）。

    真接口是全量扫条目、逐 key 做子串判断；一万次查询那样跑要几分钟。
    这里反过来：先把查询的所有 2~N 字子串列出来，再去倒排表里取命中条目。
    """

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries
        self.inverted: dict[str, list[int]] = collections.defaultdict(list)
        self.max_key = 1
        for index, item in enumerate(entries):
            for key in item["keys"]:
                self.inverted[key].append(index)
                self.max_key = max(self.max_key, len(key))

    def match(self, query: str, max_hits: int = 12) -> list[tuple[dict, float]]:
        q = str(query or "").lower()
        if not q.strip():
            return []
        found = self._count_hits(q)
        scored: list[tuple[dict, float]] = []
        for index, item in enumerate(self.entries):
            hits = found.get(index, 0)
            title = str(item["title"])
            title_hit = len(title) >= 2 and title.lower() in q
            if not hits and not title_hit:
                continue
            score = float(hits) + (TITLE_BONUS + len(title) if title_hit else 0.0)
            scored.append((item, score))
        scored.sort(
            key=lambda pair: (
                -PRIORITY_RANK.get(pair[0]["priority"], 0),
                -pair[1],
                pair[0]["title"],
            )
        )
        return scored[:max_hits]

    def _count_hits(self, q: str) -> dict[int, int]:
        """查询的每个子串去倒排表里查一次，累计每条条目命中了几个 key。"""
        found: dict[int, int] = {}
        seen: set[str] = set()
        for start in range(len(q)):
            for size in range(2, self.max_key + 1):
                part = q[start : start + size]
                if part in seen:
                    continue
                seen.add(part)
                for index in self.inverted.get(part, ()):
                    found[index] = found.get(index, 0) + 1
        return found


def check_matcher(entries: list[dict], matcher: Matcher, sample: int = 200) -> None:
    """抽样对拍：加速版与真实现必须给出同样的结果，否则评测数字不算数。"""
    from app.retrieval.store import Entry, match_entries

    real = [
        Entry(
            id=item["id"],
            title=item["title"],
            kind=item["kind"],
            priority=item["priority"],
            keys=item["keys"],
            fuzzy=item["fuzzy"],
            semantic=item["semantic"],
            body=item["body"],
        )
        for item in entries
    ]
    queries = [f"{item['title']}是什么" for item in entries[: sample // 2]]
    queries += [str(item["body"])[:18] for item in entries[: sample // 2]]
    mismatch = 0
    for query in queries:
        fast = [item["id"] for item, _ in matcher.match(query)]
        slow = [hit["id"] for hit in match_entries(real, query)]
        if fast != slow:
            mismatch += 1
    print(
        f"对拍 {len(queries)} 条：{'一致' if not mismatch else f'{mismatch} 条不一致（评测不可信）'}"
    )
    if mismatch:
        raise SystemExit(1)


def eval_titles(entries: list[dict], matcher: Matcher, max_hits: int = 12) -> None:
    stats: collections.Counter = collections.Counter()
    misses: list[str] = []
    for item in entries:
        if item["kind"] != "term":
            continue
        for query in (
            f"{item['title']}是什么",
            f"给我讲讲{item['title']}",
            f"{item['title']}的设定",
        ):
            hits = matcher.match(query, max_hits=max_hits)
            stats["queries"] += 1
            if hits and hits[0][0]["title"] == item["title"]:
                stats["top1"] += 1
            if any(hit[0]["title"] == item["title"] for hit in hits):
                stats["hit"] += 1
            elif len(misses) < 12:
                misses.append(f"{query} → {hits[0][0]['title'] if hits else '（空）'}")
    total = max(1, stats["queries"])
    print(
        f"标题问法 n={stats['queries']}  top1 {stats['top1'] / total:.1%}  "
        f"前12命中 {stats['hit'] / total:.1%}"
    )
    for line in misses:
        print(f"   漏：{line}")


def _tally(bucket: collections.Counter, docs: list[str], doc: str) -> None:
    """记一次查询的结果：有没有中、第一名是不是它。"""
    bucket["queries"] += 1
    if not docs:
        bucket["miss"] += 1
        return
    if docs[0] == doc:
        bucket["top1"] += 1
    if doc in docs:
        bucket["hit"] += 1


def eval_queries(rows: list[dict], matcher: Matcher, max_hits: int = 12) -> None:
    stats: collections.Counter = collections.Counter()
    per_cat: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_type: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for row in rows:
        doc = doc_path(row.get("doc_id", ""))
        category = str(row.get("category") or "misc")
        short_title = doc_title(doc)[:4]
        for query in row.get("queries") or []:
            docs = [doc_path(item["doc_id"]) for item, _ in matcher.match(query, max_hits=max_hits)]
            kind = "点名型" if short_title and short_title in query else "摘录型"
            for bucket in (stats, per_cat[category], per_type[kind]):
                _tally(bucket, docs, doc)
    total = max(1, stats["queries"])
    print(
        f"语料自带问法 n={stats['queries']}  前12命中 {stats['hit'] / total:.1%}  "
        f"top1 {stats['top1'] / total:.1%}  一条没中 {stats['miss'] / total:.1%}"
    )
    for label, bucket in per_type.items():
        n = max(1, bucket["queries"])
        print(
            f"   {label} n={bucket['queries']:<5} 前12 {bucket['hit'] / n:.1%}  "
            f"top1 {bucket['top1'] / n:.1%}  一条没中 {bucket['miss'] / n:.1%}"
        )
    for category, bucket in sorted(per_cat.items(), key=lambda kv: -kv[1]["queries"]):
        n = max(1, bucket["queries"])
        print(
            f"   {category:<9} n={bucket['queries']:<5} 前12 {bucket['hit'] / n:.1%}  top1 {bucket['top1'] / n:.1%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="语料库离线体检")
    parser.add_argument("--docs", default=str(DEFAULT_DOCS))
    parser.add_argument("--queries", default="", help="老库的 corpus_v2.jsonl（可选，含合成问法）")
    parser.add_argument("--limit", type=int, default=0, help="自带问法最多评多少条语料（0=全部）")
    parser.add_argument("--max-df", type=float, default=DEFAULT_MAX_DF_RATIO)
    parser.add_argument("--max-keys", type=int, default=DEFAULT_MAX_KEYS)
    args = parser.parse_args()

    docs = Path(args.docs)
    if not docs.exists():
        print(f"找不到语料：{docs}")
        return 1
    rows = load_rows(str(docs))
    entries = build_entries(rows, max_df_ratio=args.max_df, max_keys=args.max_keys)
    print(
        f"语料 {len(rows)} 块 → 条目 {len(entries)} 条 / 标题 {len({e['title'] for e in entries})} 个"
    )
    matcher = Matcher(entries)
    check_matcher(entries, matcher)
    eval_titles(entries, matcher)
    if not args.queries:
        print("（没给 --queries，跳过'语料自带问法'那一项）")
        return 0
    query_rows = load_rows(args.queries)
    if args.limit:
        query_rows = query_rows[: args.limit]
    eval_queries(query_rows, matcher)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
