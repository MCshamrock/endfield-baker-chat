"""retrieval/corpus.py —— 把老后端的 RAG 语料改写成世界书条目（只做数据，不参与打分）。

老后端的检索是向量检索：微调过的 bge 模型 + `index-endfield-v2`（4408 块语料），
跑在构建机的 5921 / 5922 两个实例上，新后端通过 `rag_client.py` 远程要结果。

新版不再走向量（`app/retrieval/` 是纯词法三档打分），但那份语料本身是多年整理的
设定与剧情文本，扔了可惜。所以这里做的是"换库不换算法"：

  一块语料（约 200 字） = 一条世界书条目
  标题  ← 语料文件名（如「世代摇篮」「刀剑无名·其一」）
  正文  ← 语料原文，**一个字都不改**（只去掉开头那行〔【标题】〕）
  keys  ← 从语料自身挖出来的名词（标题、分节标题、说话人、出现过的实体词）

三条规矩，改之前先读：

  1. **正文不改写**。摘写会掺进人的理解，检索到的东西和游戏原文就对不上了。
  2. **keys 来自语料自己**。上一版那 147 条是手写的触发词，语料一更新就跟不上；
     这里全部从语料统计出来，重跑一次脚本就同步了。
  3. **太常见的词不当 keys**。出现在 2% 以上块里的词（管理员、终末地……）几乎每句
     都有，拿它当触发词等于"什么都命中"，会把真正相关的那条挤出去。
"""

import collections
import json
import re

# 语料每块开头都有一行【分节标题】，比如【主线任务·序章·硬启动·第2段】【世代摇篮】
_HEADER = re.compile(r"^【([^】]{1,60})】")

# 标题/分节标题的切分符：刀剑无名·其一 → 刀剑无名 / 其一
_SPLIT = re.compile(r"[·・\-—－_/、，。；;：:（）()\[\]【】《》「」『』“”\"'\s]+")

# 剧情里说话人写成「名字：台词」，这是最可靠的实体名单来源
_SPEAKER = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{1,6})[：:]")

# 单字不当触发词。剧情里真有单字名字（言、峰、诀），但「方」「年」「望」这类单字
# 会撞进任意一句话里 —— 检索是子串匹配，一个「方」能让半本语料都命中。
MIN_TERM_LEN = 2

# 这些词满篇都是、或者只是分节用的编号词，当触发词只会添乱
# （df 上限能挡掉大部分，这里再兜一层最典型的）
STOP_WORDS = {
    "管理员",
    "终末地",
    "终末地工业",
    "塔卫二",
    "博士",
    "各位",
    "序章",
    "尾声",
    "开篇",
    "终章",
    "其一",
    "其二",
    "其三",
    "其四",
    "其五",
    "其六",
    "内容",
    "人群",
    "兴趣",
    "专长",
    "外貌",
    "性别",
    "少年",
    "弟子",
    "先师",
    "内阁",
    "六部",
    "余等",
    "年关",
    "图注",
    "上篇",
    "下篇",
}

# 语料里的 category → 世界书的 kind（决定索引里归到哪一组、预取时排第几）
KIND_BY_CATEGORY = {
    "archive": "term",
    "material": "term",
    "misc": "term",
}

# 中枢档案与材料都是"设定条目"（词条式，一问就该答这个），一起放 high；
# 剧情条目按 mid；杂项（剧情顺序表、整理稿）最低。
# 注意 priority 在排序里优先于分数，所以档案和材料必须同级 —— 否则"星门菌"这种
# 材料条目会被提到「星门」的档案条目永远压在下面。
PRIORITY_BY_CATEGORY = {"archive": "high", "material": "high", "misc": "low"}

# df 上限（出现在多少比例的块里就不当触发词）。2% 是拿语料自带的 1 万条问法调出来的：
# 再放宽会让"超域"这类词把一堆无关剧情顶进候选，标题问法的命中率反而掉。
DEFAULT_MAX_DF_RATIO = 0.02
DEFAULT_MAX_KEYS = 8


def load_rows(path: str) -> list[dict]:
    """读老 RAG 的语料导出（每行一个 JSON：id / doc_id / category / title / text）。"""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def split_terms(text: object) -> list[str]:
    """把标题切成候选词：2~12 字，太短太长都不要。"""
    return [part for part in _SPLIT.split(str(text or "")) if 2 <= len(part) <= 12]


def doc_path(doc_id: str) -> str:
    """`世界任务/刀剑无名·其一.md#3` → `世界任务/刀剑无名·其一.md`（一块语料属于哪个文件）。"""
    return str(doc_id or "").rsplit("#", 1)[0]


def doc_title(path: str) -> str:
    """`世界任务/刀剑无名·其一.md` → `刀剑无名·其一`。"""
    name = path.rsplit("/", 1)[-1]
    return name[: -len(".md")] if name.endswith(".md") else name


def _collect_docs(rows: list[dict]) -> dict[str, dict]:
    """文件路径 → {category, stem}。"""
    docs: dict[str, dict] = {}
    for row in rows:
        path = doc_path(row.get("doc_id", ""))
        doc = docs.setdefault(path, {"category": row.get("category") or "misc"})
        doc["stem"] = doc_title(path)
    return docs


def _collect_vocabulary(rows: list[dict], docs: dict[str, dict]) -> collections.Counter:
    """候选词表：文件标题 + 分节标题 + 说话人（单字的丢掉）。"""
    vocab: collections.Counter = collections.Counter()
    for doc in docs.values():
        vocab.update([doc["stem"], *split_terms(doc["stem"])])
    for row in rows:
        text = str(row.get("text") or "")
        header = _HEADER.match(text)
        if header:
            vocab.update(split_terms(header.group(1)))
        vocab.update(name for name in _SPEAKER.findall(text) if len(name) >= MIN_TERM_LEN)
    for term in [item for item in vocab if len(item) < MIN_TERM_LEN]:
        del vocab[term]
    return vocab


def _pick_keys(stem: str, section: list[str], present: list[str], max_keys: int) -> list[str]:
    """拼触发词：标题打头，再补分节与小节里出现过的实体词。"""
    keys: list[str] = []
    for term in [stem, *section, *present]:
        if len(term) < MIN_TERM_LEN or term in keys or len(keys) >= max_keys:
            continue
        keys.append(term)
    return keys


def _make_entry(
    index: int,
    row: dict,
    docs: dict[str, dict],
    df: dict[str, int],
    keywords: list[str],
    max_keys: int,
) -> dict | None:
    """一块语料 → 一条条目。正文为空就返回 None（空条目进库只会占预算）。"""
    doc = docs[doc_path(row.get("doc_id", ""))]
    text = str(row.get("text") or "")
    header = _HEADER.match(text)
    # 分节标题里也会有编号词（「其一」「序章」），跟触发词用同一套过滤
    section = (
        [term for term in split_terms(header.group(1)) if term not in STOP_WORDS] if header else []
    )
    body = _HEADER.sub("", text, count=1).strip()
    if not body:
        return None

    # 块里出现过的实体词，越少见越靠前（少见的词才是"这个词条专属"的）
    present = sorted(
        (term for term in keywords if term in text), key=lambda term: (df[term], -len(term))
    )
    category = str(doc["category"])
    return {
        "id": f"e{index:04d}",
        "title": str(doc["stem"])[:120],
        "kind": KIND_BY_CATEGORY.get(category, "event"),
        "priority": PRIORITY_BY_CATEGORY.get(category, "mid"),
        "keys": _pick_keys(str(doc["stem"]), section, present, max_keys),
        "fuzzy": [],
        "semantic": body[:600],
        "body": body,
        "doc_id": row.get("doc_id", ""),
    }


def build_entries(
    rows: list[dict],
    *,
    max_df_ratio: float = DEFAULT_MAX_DF_RATIO,
    max_keys: int = DEFAULT_MAX_KEYS,
) -> list[dict]:
    """语料块 → 条目列表（纯函数，不碰数据库，便于单测和 dry-run）。"""
    if not rows:
        return []

    docs = _collect_docs(rows)
    vocab = _collect_vocabulary(rows, docs)
    # 文档频率：一个词出现在多少块语料里
    df = {term: sum(1 for row in rows if term in str(row.get("text") or "")) for term in vocab}
    limit = max(3, int(len(rows) * max_df_ratio))
    keywords = [term for term in vocab if df[term] <= limit and term not in STOP_WORDS]

    entries: list[dict] = []
    for index, row in enumerate(rows, 1):
        item = _make_entry(index, row, docs, df, keywords, max_keys)
        if item is not None:
            entries.append(item)
    return entries


def summarize(entries: list[dict]) -> dict[str, object]:
    """给 CLI 打印用的一页统计。"""
    titles = {item["title"] for item in entries}
    chars = sum(len(str(item["body"])) for item in entries)
    return {
        "entries": len(entries),
        "titles": len(titles),
        "chars": chars,
        "avg_keys": round(sum(len(item["keys"]) for item in entries) / max(1, len(entries)), 2),
        "kinds": dict(collections.Counter(item["kind"] for item in entries)),
        "priorities": dict(collections.Counter(item["priority"] for item in entries)),
    }


__all__ = [
    "DEFAULT_MAX_DF_RATIO",
    "DEFAULT_MAX_KEYS",
    "KIND_BY_CATEGORY",
    "MIN_TERM_LEN",
    "PRIORITY_BY_CATEGORY",
    "STOP_WORDS",
    "build_entries",
    "doc_path",
    "doc_title",
    "load_rows",
    "split_terms",
    "summarize",
]
