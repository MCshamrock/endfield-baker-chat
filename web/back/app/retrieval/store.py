"""retrieval/store.py —— 世界书的内存索引与三个入口。

对应聊天终端内核（`tavern_core.js`）里的：
  matchEntries   911   ← 旧版预取（关键词子串 + 优先级排序），**每轮自动用的就是它**
  searchEntries  947   ← 三档检索（给"模型自己调工具"用）
  buildIndexText 973   ← 索引文本（按 kind 分组 + 常驻条目）

数据来源是 `worldbook` 表。现在这份数据是从老后端的 RAG 语料改写的
（`scripts/import_rag_corpus.py`，4408 块 ≈ 90 万字 ≈ 600k token），**整份装进内存**
（约 1 MB 正文），每次请求不再查库。这和终端"打开世界书→读索引→缓存"的做法一致。

两条从终端继承的规矩：
  1. **常驻条目（constant）不进检索**：它们每轮无条件在场，再让它们占检索名额是浪费。
  2. **检索结果不落盘**：只在本轮上下文里存在，不进聊天记录。
"""

import logging
from dataclasses import dataclass, field
from typing import TypedDict

from app.retrieval.score import PRIORITY_RANK, Hit, score_entry, sort_hits
from app.retrieval.text import normalize_mode

logger = logging.getLogger("baker.retrieval")

# 索引按 kind 分组，顺序固定（和终端 INDEX_GROUP 一致）
INDEX_GROUP = {
    "term": "专有名词",
    "place": "地点",
    "person": "人物",
    "event": "事件",
    "rule": "规则与习俗",
}

# 不进索引目录的 kind。剧情条目（task / 主线支线 / 干员频道）一条一块语料，
# 三百多个标题列成目录每轮都要付钱，而模型拿这份目录也判断不了什么 ——
# 它们照样参与预取，只是不列在目录里。
INDEX_SKIP_KINDS = {"event"}

# 标题整串出现在提问里 → 用户就是在问这条，给一个压过"关键词撞车"的加分。
# 三档打分里本来就有这条（exact 档标题命中 +30），预取这条路以前漏了：
# 没有它时「超域试验场在哪」会先被「超域」抢走（两条都命中、只能比标题排序）。
# 一字的标题（例如某个只有单名的角色）不加：'言' 这种会撞进'语言'里。
# 另外还要**按标题长度再加分**：语料里「超域」「超域试验场」「超域实验禁令」互为前缀，
# 问「超域试验场」时三条的标题都"在提问里"，只有更长的那条才是用户真正指的。
TITLE_BONUS = 30.0


class SearchResult(TypedDict):
    """一次检索的返回。字段名跟内核的 searchEntries 一致。"""

    query: str
    mode: str
    hits: list[Hit]
    constants: list[str]
    indexSize: int


# 每条正文的估算系数（中文约 1.5 字符 / token），和 prompt/budget.py 保持一致
_CHARS_PER_TOKEN = 1.5


@dataclass(slots=True)
class Entry:
    """一条世界书条目。字段名对应内核里的 t/k/s/fz/p/c。"""

    id: str
    title: str
    kind: str = "term"
    priority: str = "mid"
    keys: list[str] = field(default_factory=list)
    fuzzy: list[str] = field(default_factory=list)
    semantic: str = ""
    body: str = ""
    constant: bool = False

    def score(self, query: str, mode: str) -> Hit | None:
        return score_entry(
            entry_id=self.id,
            title=self.title,
            keys=self.keys,
            fuzzy=self.fuzzy,
            semantic=self.semantic,
            kind=self.kind,
            priority=self.priority,
            query=query,
            mode=mode,
        )


def search_entries(
    entries: list[Entry], query: str, mode: object = "exact", max_hits: int = 8
) -> SearchResult:
    """三档检索唯一入口。常驻条目一律剔除。"""
    normalized = normalize_mode(mode)
    hits = [
        scored
        for entry in entries
        if not entry.constant
        for scored in [entry.score(query, normalized)]
        if scored is not None
    ]
    return {
        "query": query,
        "mode": normalized,
        "hits": sort_hits(hits)[:max_hits],
        "constants": [entry.id for entry in entries if entry.constant],
        "indexSize": len(entries),
    }


def match_entries(entries: list[Entry], text: str, max_hits: int = 12) -> list[Hit]:
    """旧版预取：中文不分词，直接**关键词子串**匹配，按优先级再按命中数排序。

    这是每轮真正在起作用的那条路（终端 §2.4 路径 A）。
    它比三档检索糙，但快且不依赖模型判断；错别字/语义那两档只在模型主动调工具时才有。

    标题整串命中另加 `TITLE_BONUS`（再按标题长度加权）：语料里有「超域」「超域试验场」
    「超域实验禁令」这种互为前缀的标题，只数命中词数的话问「超域试验场」会先命中「超域」。
    加分让"标题就在提问里、而且越长越具体"的那条排前面 —— 这跟三档里 exact 档 +30 同源。
    """
    q = str(text or "").lower()
    if not q.strip():
        return []

    scored: list[Hit] = []
    for entry in entries:
        if entry.constant:
            continue
        hit_keys: list[str] = []
        for key in entry.keys:
            kk = str(key).lower()
            if kk and kk in q:
                hit_keys.append(key)
        title = str(entry.title or "")
        title_hit = len(title) >= 2 and title.lower() in q
        if not hit_keys and not title_hit:
            continue
        score = float(len(hit_keys))
        if title_hit:
            score += TITLE_BONUS + len(title)
        scored.append(
            {
                "id": entry.id,
                "title": entry.title,
                "semantic": entry.semantic,
                "keys": entry.keys,
                "kind": entry.kind,
                "priority": entry.priority,
                "score": score,
                "hitKeys": hit_keys,
            }
        )

    scored.sort(
        key=lambda hit: (
            -PRIORITY_RANK.get(hit["priority"], 0),
            -hit["score"],
            hit["title"],
        )
    )
    return scored[:max_hits]


def _index_line(entry: Entry) -> str:
    """索引里的一行：`e001 超域`。

    只放编号和标题，**不放语义描述和关键词**。理由：
    这份索引的读者是"模型"，而它拿它干不了什么 —— 检索是我们自己用 `match_entries`
    按关键词跑的，模型不需要靠这行字判断相关度。多放的每一个字都是每轮都要付的钱。
    真正要读正文时走 `prefetch` 按预算取，那才是正文该出现的地方。
    """
    return f"{entry.id} {entry.title}"


def _index_entries(entries: list[Entry]) -> list[Entry]:
    """目录里要列哪些条目：剧情条目不列，同一标题只留第一条。"""
    listed: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.kind in INDEX_SKIP_KINDS:
            continue
        mark = (entry.kind, str(entry.title))
        if mark in seen:
            continue
        seen.add(mark)
        listed.append(entry)
    return listed


def build_index_text(
    entries: list[Entry], world_name: str = "", brief: str = ""
) -> tuple[str, str]:
    """索引文本 + 常驻正文。

    索引一行一条（编号 + 标题，约 5–10 token/条）—— 这是"模型知道有这么条设定"的价格。
    常驻条目（世界名/时代/基本法则）正文直接拼在索引后面，不进索引。

    一条语料一块条目之后，同一个标题会有好几条（刀剑无名·其一 有 12 块），
    所以这里**按（kind, 标题）去重**：目录只列一次，正文按需预取。
    """
    listed = _index_entries(entries)

    groups: dict[str, list[str]] = {}
    for entry in listed:
        groups.setdefault(INDEX_GROUP.get(entry.kind, "专有名词"), []).append(_index_line(entry))

    lines = [f"【世界书检索索引 · {world_name or '终末地'} · 共 {len(listed)} 条】"]
    if brief:
        lines.append(brief)
    for group in INDEX_GROUP.values():
        items = groups.get(group)
        if not items:
            continue
        lines.append(f"〔{group}〕")
        lines.extend(items)

    constants = [entry for entry in entries if entry.constant]
    constants_text = ""
    if constants:
        joined = "\n\n".join(f"（{entry.title}）\n{entry.body}" for entry in constants)
        constants_text = "【世界书·常驻】\n" + joined

    return "\n".join(lines), constants_text


def prefetch(
    entries: list[Entry],
    user_texts: list[str],
    *,
    budget_tokens: int = 2000,
    max_candidates: int = 12,
    max_per_title: int = 2,
) -> str:
    """每轮自动预取：最近几条用户消息 → 关键词匹配 → 按预算取正文。

    三个细节都是从终端搬的：
      * 查询用**最近 3 条**用户消息拼起来（只用最新一条会漏掉连续追问）；
      * 候选最多 12 条，然后**按预算逐条累加，超了就跳过**，绝不把上下文顶爆；
      * 正文包成 `（标题）正文`，不写来源、不说"根据资料" —— 让模型当背景知识用。

    一条语料一块条目之后，命中常常是同一标题的好几块（问「四号谷地」会中它的两块）。
    所以这里**按标题并成一段**：标题写一次，几块正文接在后面 —— 不然模型会看到
    一串重复的（四号谷地）（四号谷地）。

    而且**同一篇最多取 `max_per_title` 块**：角色档案这种一篇几十块的条目，
    不设上限时一句话就能把 12 个候选全占满，同轮问到的另一个话题（比如「明晦炉」）
    连候选都进不去。
    """
    query = "\n".join([text for text in user_texts if text.strip()][-3:])
    if not query.strip():
        return ""

    by_id = {entry.id: entry for entry in entries}
    grouped: list[tuple[str, list[str]]] = []
    per_title: dict[str, int] = {}
    used_tokens = 0.0
    # 候选窗口要开大：一篇几十块的条目（角色档案）会把前几十个位置全占了，
    # 同轮问到的第二个话题连"候选"都进不来。截断交给下面的 per_title 与预算。
    for hit in match_entries(entries, query, max_hits=len(entries)):
        entry = by_id.get(str(hit["id"]))
        if entry is None or not entry.body.strip():
            continue
        if per_title.get(entry.title, 0) >= max_per_title:
            continue
        cost = len(entry.body) / _CHARS_PER_TOKEN
        if used_tokens + cost > budget_tokens:
            continue  # 不是停，是跳过这一条再看后面更短的
        per_title[entry.title] = per_title.get(entry.title, 0) + 1
        if grouped and grouped[-1][0] == entry.title:
            grouped[-1][1].append(entry.body)
        else:
            grouped.append((entry.title, [entry.body]))
        used_tokens += cost

    if not grouped:
        return ""
    picked = [f"（{title}）" + "\n".join(bodies) for title, bodies in grouped]
    blocks = sum(len(bodies) for _, bodies in grouped)
    logger.info("预取 %s 段 / %s 块世界书（约 %s token）", len(picked), blocks, int(used_tokens))
    return "\n\n".join(picked)


def find_by_keys(entries: list[Entry], entry_ids: list[str]) -> list[Entry]:
    """按编号取正文（对应内核的 loadEntries / worldbook_read 工具）。"""
    wanted = set(entry_ids)
    return [entry for entry in entries if entry.id in wanted]


__all__ = [
    "INDEX_GROUP",
    "Entry",
    "build_index_text",
    "find_by_keys",
    "match_entries",
    "prefetch",
    "search_entries",
]
