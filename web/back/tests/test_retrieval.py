"""tests/test_retrieval.py —— 三档检索 + 预取（算法移植的对照测试）。

这组测试的作用相当于终端那边的 `test_tavern_core.js`：
算法是从 `tavern_core.js` 逐行搬过来的，这些断言保证"搬过来没走样"。
改动打分公式前先看这里 —— 任何一条失败都意味着三端结果会不一致。
"""

from app.retrieval.store import (
    Entry,
    build_index_text,
    match_entries,
    prefetch,
    search_entries,
)
from app.retrieval.text import bigrams, edit_distance, norm_text, normalize_mode


def entry(eid: str, title: str, keys: list[str], semantic: str = "", **kw: object) -> Entry:
    fuzzy_raw = kw.get("fuzzy")
    fuzzy = [str(item) for item in fuzzy_raw] if isinstance(fuzzy_raw, list) else []
    return Entry(
        id=eid,
        title=title,
        keys=keys,
        semantic=semantic,
        body=str(kw.get("body", "正文")),
        kind=str(kw.get("kind", "term")),
        priority=str(kw.get("priority", "mid")),
        fuzzy=fuzzy,
        constant=bool(kw.get("constant", False)),
    )


def _score(e: Entry, query: str, mode: str) -> float:
    result = e.score(query, mode)
    return float(result["score"]) if result else 0.0


# ---------------------------------------------------------------- 基础函数
def test_norm_text() -> None:
    assert norm_text("  Hello   World ") == "hello world"
    assert norm_text(None) == ""


def test_bigrams_strip_punctuation() -> None:
    assert bigrams("超域！") == {"超域"}
    assert bigrams("a") == {"a"}
    assert bigrams("") == set()


def test_edit_distance_with_limit() -> None:
    assert edit_distance("超域", "超域", 1) == 0
    assert edit_distance("超域", "超愈", 1) == 1
    # 超过上限直接给 limit+1，不算完整距离
    assert edit_distance("超域", "完全不同", 1) == 2


def test_normalize_mode_aliases() -> None:
    assert normalize_mode("精确") == "exact"
    assert normalize_mode("FUZZY") == "fuzzy"
    assert normalize_mode("语义") == "semantic"
    assert normalize_mode("乱七八糟") == "exact"  # 不认识的一律当 exact


# ---------------------------------------------------------------- 三档打分
def test_exact_hits_key_and_title() -> None:
    e = entry("e001", "超域", ["超域", "亚空间"])
    # 查询里同时出现 key 和 title：12（key）+ 30（title 整串命中）
    assert _score(e, "超域是什么", "exact") == 42
    # 只命中 key、不命中 title
    assert _score(e, "亚空间在哪", "exact") == 13  # 10 + min(3,8)
    assert _score(e, "今天吃什么", "exact") == 0.0


def test_fuzzy_tolerates_typo() -> None:
    e = entry("e001", "超域", ["超域"], fuzzy=["超愈"])
    # 「超愈」是 fuzzy 里给的易错写法，查询原样命中
    assert _score(e, "超愈", "fuzzy") > 0
    # 单字之差也能命中（keys 里是「超域」，查询「超预」）。
    # 注意：模糊档按标点/空格切词，所以查询里要有分隔（整句不切词是内核的既定行为）。
    assert _score(e, "超预 是什么", "fuzzy") > 0


def test_semantic_returns_candidates_without_threshold() -> None:
    """语义档故意不设阈值：返回候选菜单，由模型看着 semantic 挑。"""
    e = entry("e002", "侵蚀", ["侵蚀"], semantic="会解离或反演物质与时间的有害现象")
    # 语义档靠字符二元组重叠，所以查询必须和条目有共同字面（这正是"不设硬阈值"的前提）
    assert _score(e, "侵蚀会解离东西吗", "semantic") > 0


def test_semantic_prefers_related_entry() -> None:
    related = entry("e002", "侵蚀", ["侵蚀"], semantic="会解离或反演物质与时间的有害现象")
    unrelated = entry("e003", "天使分类编码", ["天使"], semantic="用来查询天使弱点的编码标准")
    query = "侵蚀会把东西解离吗"
    assert _score(related, query, "semantic") > _score(unrelated, query, "semantic")


# ---------------------------------------------------------------- 检索入口
def test_search_excludes_constants_and_sorts() -> None:
    # 两条都不靠 title 加分（title 不整串出现在查询里），分数相同 → 优先级决定先后
    entries = [
        entry("e001", "甲", ["超域"], priority="mid"),
        entry("e002", "乙", ["超域"], priority="high"),
        entry("e999", "世界名", ["超域"], priority="high", constant=True),
    ]
    result = search_entries(entries, "超域", "exact")
    ids = [hit["id"] for hit in result["hits"]]
    assert "e999" not in ids, "常驻条目不该占检索名额"
    assert result["constants"] == ["e999"]
    # 同分时高优先级在前
    assert ids[0] == "e002"


def test_search_limits_hits() -> None:
    entries = [entry(f"e{i:03d}", f"条目{i}", ["龙门"]) for i in range(10)]
    assert len(search_entries(entries, "龙门", "exact", max_hits=3)["hits"]) == 3


# ---------------------------------------------------------------- 预取
def test_match_entries_orders_by_priority_then_hits() -> None:
    entries = [
        entry("e001", "低优先", ["龙门"], priority="low"),
        entry("e002", "高优先", ["龙门"], priority="high"),
        entry("e003", "双命中", ["龙门", "通行证"], priority="high"),
    ]
    hits = match_entries(entries, "去龙门要带通行证")
    assert [hit["id"] for hit in hits][:2] == ["e003", "e002"]


def test_prefetch_respects_budget_and_skips_long_entries() -> None:
    short = entry("e001", "短的", ["龙门"], body="短" * 30)
    huge = entry("e002", "长的", ["龙门"], body="长" * 30000)
    text = prefetch([short, huge], ["去龙门"], budget_tokens=100)
    assert "短的" in text
    assert "长的" not in text, "超预算的条目应当被跳过，而不是把上下文顶爆"


def test_prefetch_uses_last_three_user_messages() -> None:
    """只用最新一条会漏掉连续追问，所以查询是多条拼起来的。"""
    e1 = entry("e001", "通行证", ["通行证"])
    e2 = entry("e002", "龙门", ["龙门"])
    text = prefetch([e1, e2], ["第一句无关", "第二句提到龙门", "第三句继续问"])
    assert "龙门" in text


# ---------------------------------------------------------------- 索引
def test_index_groups_by_kind_and_marks_constant() -> None:
    entries = [
        entry("e001", "超域", ["超域"], semantic="深度读数1的时空", kind="term"),
        entry("e002", "武陵", ["武陵"], semantic="一座城", kind="place"),
        entry("e999", "终末地", ["终末地"], kind="term", constant=True),
    ]
    text, constants = build_index_text(entries, "终末地")
    assert "〔专有名词〕" in text and "〔地点〕" in text
    # 索引只留编号 + 标题：它是"目录"，不是正文，也不是给模型做相关度判断的材料
    assert "e001 超域" in text
    assert "深度读数1的时空" not in text
    assert "｜" not in text
    assert "（终末地）" in constants


def test_index_skips_empty_groups() -> None:
    entries = [entry("e001", "超域", ["超域"], kind="term")]
    text, _ = build_index_text(entries)
    assert "〔人物〕" not in text
