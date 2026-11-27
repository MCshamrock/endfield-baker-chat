"""tests/test_rag_corpus.py —— 语料改写（老 RAG 库 → 世界书条目）的对照测试。

这组测试守的是"换库"这件事本身：
  * 语料块怎么变成条目（标题取文件名、正文不动、分节标题剥掉）；
  * 触发词从哪来（标题 / 分节 / 说话人），哪些词必须被挡掉（太常见、单字）；
  * 条目的 kind / priority 怎么定；
  * 预取时「标题在提问里」「同一标题多块」这两件事的处理。
数据侧的基准是 `back/data/rag_corpus/docs.jsonl`（老后端向量库的原样导出）。
"""

import json
from pathlib import Path

from app.retrieval.corpus import build_entries, doc_path, doc_title, split_terms
from app.retrieval.store import Entry, build_index_text, match_entries, prefetch

CORPUS = Path(__file__).resolve().parent.parent / "data" / "rag_corpus" / "docs.jsonl"


def row(doc_id: str, category: str, text: str, title: str = "") -> dict:
    return {
        "id": 0,
        "doc_id": doc_id,
        "category": category,
        "title": title or doc_title(doc_path(doc_id)),
        "text": text,
    }


def to_entries(built: list[dict]) -> list[Entry]:
    return [
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
        for item in built
    ]


# ---------------------------------------------------------------- 文件切分
def test_doc_path_and_title() -> None:
    assert doc_path("世界任务/刀剑无名·其一.md#3") == "世界任务/刀剑无名·其一.md"
    assert doc_title("世界任务/刀剑无名·其一.md") == "刀剑无名·其一"
    assert split_terms("刀剑无名·其一") == ["刀剑无名", "其一"]


# ---------------------------------------------------------------- 条目化
def test_build_entries_strips_section_header_only() -> None:
    """正文只去掉开头那行【分节标题】，其余一个字都不许动。"""
    rows = [row("中枢档案/超域.md#0", "archive", "【超域】超域是深度读数为1的时空。")]
    built = build_entries(rows)
    assert len(built) == 1
    item = built[0]
    assert item["title"] == "超域"
    assert item["body"] == "超域是深度读数为1的时空。"
    assert item["doc_id"] == "中枢档案/超域.md#0"


def test_build_entries_keys_cover_title_and_section() -> None:
    rows = [
        row("中枢档案/超域.md#0", "archive", "【超域】超域是深度读数为1的时空。"),
        row("中枢档案/超域.md#1", "archive", "【超域】超域试验场建在供能高地。"),
    ]
    built = build_entries(rows)
    assert built[0]["keys"][0] == "超域"
    # 同一标题的两块都带标题当触发词，问「超域」时两块都能被捞到
    assert "超域" in built[1]["keys"]


def test_common_words_are_not_keys() -> None:
    """df 超过上限的词不当触发词：否则任何一句话都能命中一堆条目。"""
    rows = [
        row(f"中枢档案/条目{i}.md#0", "archive", f"【条目{i}】管理员在这里记录条目{i}。")
        for i in range(6)
    ]
    built = build_entries(rows, max_df_ratio=0.5)  # limit=3，管理员出现在 6 块里 → 必被挡
    assert all("管理员" not in item["keys"] for item in built)


def test_single_char_title_is_not_a_key() -> None:
    """语料里有个单标题「祀」，单字当触发词会撞进任意一句话。"""
    rows = [row("干员频道/祀.md#0", "channel", "【祀频道】祀：今天也来坐坐？")]
    built = build_entries(rows)
    assert "祀" not in built[0]["keys"]
    assert built[0]["title"] == "祀"


def test_kind_and_priority_mapping() -> None:
    rows = [
        row("中枢档案/超域.md#0", "archive", "【超域】正文"),
        row("材料/星门菌.md#0", "material", "【星门菌】正文"),
        row("主线任务/序章/进程·复苏.md#0", "main", "【主线任务】正文"),
        row("剧情顺序.md#0", "misc", "正文"),
    ]
    built = build_entries(rows)
    by_title = {item["title"]: item for item in built}
    assert by_title["超域"]["kind"] == "term" and by_title["超域"]["priority"] == "high"
    assert by_title["星门菌"]["kind"] == "term" and by_title["星门菌"]["priority"] == "high"
    assert by_title["进程·复苏"]["kind"] == "event" and by_title["进程·复苏"]["priority"] == "mid"
    assert by_title["剧情顺序"]["priority"] == "low"


# ---------------------------------------------------------------- 检索行为
def test_title_in_query_beats_prefix_collision() -> None:
    """「超域试验场」和「超域」互为前缀：问前者时前者必须排第一。"""
    entries = [
        Entry(id="e0001", title="超域", keys=["超域"], body="超域正文"),
        Entry(id="e0002", title="超域试验场", keys=["超域试验场"], body="试验场正文"),
    ]
    hits = match_entries(entries, "超域试验场在哪里")
    assert hits[0]["title"] == "超域试验场"
    # 问短的仍然命短的那条
    assert match_entries(entries, "超域是什么")[0]["title"] == "超域"


def test_title_bonus_does_not_apply_to_single_char_title() -> None:
    entries = [Entry(id="e0001", title="祀", keys=["祀频道"], body="正文")]
    assert match_entries(entries, "语言是什么") == []


def test_prefetch_groups_chunks_of_same_title() -> None:
    """同一标题的多块语料要并成一段，否则模型会看到一串重复的（标题）。"""
    entries = [
        Entry(id="e0001", title="超域", keys=["超域"], body="第一块"),
        Entry(id="e0002", title="超域", keys=["超域"], body="第二块"),
    ]
    text = prefetch(entries, ["超域"], budget_tokens=500)
    assert text.count("（超域）") == 1
    assert "第一块" in text and "第二块" in text


# ---------------------------------------------------------------- 索引
def test_index_dedupes_by_title_and_skips_events() -> None:
    entries = [
        Entry(id="e0001", title="超域", keys=["超域"], kind="term"),
        Entry(id="e0002", title="超域", keys=["超域"], kind="term"),
        Entry(id="e0003", title="进程·复苏", keys=["进程·复苏"], kind="event"),
    ]
    text, _ = build_index_text(entries, "终末地")
    assert text.count("超域") == 1, "同一标题在目录里只列一次"
    assert "进程·复苏" not in text, "剧情条目不列目录"
    assert "共 1 条" in text


# ---------------------------------------------------------------- 数据本身
def test_committed_corpus_loads() -> None:
    """仓库里那份语料必须能读、字段齐全（换语料时第一道闸）。"""
    assert CORPUS.exists(), f"语料不在：{CORPUS}"
    lines = 0
    with CORPUS.open(encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            for field in ("doc_id", "category", "title", "text"):
                assert field in payload
            lines += 1
            if lines >= 20:
                break
    assert lines == 20
