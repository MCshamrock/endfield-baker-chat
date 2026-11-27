"""scripts/build_supplement_prts.py —— 从本地 PRTS 语料库生成「补充条目」。

语料库位置：`$DSH_HOME/prts-corpus/releases/<最新 release>`（PRTS.chat 的本地包，
每篇文档是一份 gzip jsonl 分片，正文在 `lines` 里）。这里只取**终末地的角色档案**
（`resource_type == "character_profile"`）：每条按「基础档案 / 人事简述 / 档案资料·N」
切段成若干条目 —— 正文是 PRTS 的官方文本，一个字不改，只把每行前面的小节前缀去掉。

产出：`data/rag_corpus/supplement_prts.jsonl`（一行一条，导入脚本会读它）。

    python scripts/build_supplement_prts.py            # 用最新 release
    python scripts/build_supplement_prts.py --check    # 只统计，不写文件
"""

import argparse
import collections
import glob
import gzip
import json
import os
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "rag_corpus" / "supplement_prts.jsonl"

# 每行前面那个小节前缀：「基础档案：」「人事简述：」「档案资料·一：」
_PREFIX = re.compile(r"^(基础档案|人事简述|档案资料·[一二三四五六七八九十]+)[：:]")

# 一节按句子切成 ~400 字一条：切点永远落在句号后，不会读到半句话。
# 为什么不整节一条：预取预算只有 2000 token，一节 1500 字（约 1000 token）会把
# 预算吃光，别的条目就挤不进来了（实测：档案一进，同轮问的「明晦炉」被挤掉）。
_CHUNK_CHARS = 400


def default_corpus_root() -> Path:
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(home) / "prts-corpus" / "releases"


def current_release(root: Path) -> Path:
    """`current.json` 指向的 release；没有就取目录里最新的一个。"""
    pointer = root / "current.json"
    if pointer.exists():
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        candidate = root / str(payload.get("release_id") or "")
        if candidate.is_dir():
            return candidate
    releases = sorted(path for path in root.iterdir() if path.is_dir())
    if not releases:
        raise SystemExit(f"语料库是空的：{root}")
    return releases[-1]


def iter_docs(pack: Path):
    for path in sorted(glob.glob(str(pack / "shards" / "*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def split_by_section(lines: list[str]) -> "collections.OrderedDict[str, list[str]]":
    """把 PRTS 的档案行按小节分组（前缀去掉）。"""
    sections: collections.OrderedDict[str, list[str]] = collections.OrderedDict()
    for raw in lines:
        text = str(raw).strip()
        if not text:
            continue
        match = _PREFIX.match(text)
        if match:
            name, text = match.group(1), text[match.end() :].strip()
        else:
            name = ""
        sections.setdefault(name, []).append(text)
    return sections


def pack_section(text: str) -> list[str]:
    """一节太长就按句号切几块（不切断句子）。"""
    if len(text) <= _CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    buffer = ""
    for piece in re.split(r"(?<=[。！？])", text):
        if not piece:
            continue
        if len(buffer) + len(piece) > _CHUNK_CHARS and buffer:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer += piece
    if buffer:
        chunks.append(buffer)
    return chunks


def document_entries(meta: dict, lines: list[str], seen: set[tuple[str, str]]) -> list[dict]:
    """一份角色档案 → 若干条目（按小节、按句子切）。"""
    title = str(meta.get("display_title") or "").split(" / ")[0].strip()
    if not title:
        return []
    entries: list[dict] = []
    for index, (section, texts) in enumerate(split_by_section(lines).items()):
        for part in pack_section("\n".join(texts)):
            mark = (title, f"{section}:{part[:20]}")
            if mark in seen:
                continue
            seen.add(mark)
            entries.append(
                {
                    "id": "",  # 最后统一编号
                    "title": title,
                    "kind": "person",
                    "priority": "high",
                    "keys": [title],
                    "fuzzy": [],
                    "semantic": part[:120],
                    "body": part,
                    "source": "prts",
                    "source_ref": meta.get("document_id", ""),
                    "doc_id": f"角色档案/{title}.md#{index}",
                }
            )
    return entries


def build(release: Path) -> list[dict]:
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for doc in iter_docs(release / "endfield_official_game"):
        meta = doc["document"]
        if meta.get("resource_type") != "character_profile":
            continue
        lines = [
            str(item.get("text", item)) if isinstance(item, dict) else str(item)
            for item in (doc.get("lines") or [])
        ]
        entries.extend(document_entries(meta, lines, seen))
    for number, item in enumerate(entries, 1):
        item["id"] = f"s{number:04d}"
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="从本地 PRTS 语料生成补充条目")
    parser.add_argument("--corpus", default=str(default_corpus_root()))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    root = Path(args.corpus)
    if not root.is_dir():
        print(f"找不到 PRTS 语料库：{root}")
        return 1
    release = current_release(root)
    entries = build(release)
    titles = {item["title"] for item in entries}
    chars = sum(len(item["body"]) for item in entries)
    print(f"release {release.name}：角色档案 {len(titles)} 人 → 条目 {len(entries)} 条 / {chars} 字")
    print(f"  样例 {entries[0]['title']} → {entries[0]['body'][:60]}…")
    if args.check:
        print("--check：没有写文件")
        return 0
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for item in entries:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"  已写出 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
