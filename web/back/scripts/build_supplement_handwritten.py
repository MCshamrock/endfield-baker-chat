"""scripts/build_supplement_handwritten.py —— 手写世界书里「RAG 语料没有的那些」→ 补充条目。

判据很硬：**标题在 base 语料正文里一次都没出现** = 这条只在我们手写库里存在
（比如「职业与元素」「协议传送规矩」这种规则/机制条目，剧情语料里不会写）。

产出 `data/rag_corpus/supplement_handwritten.jsonl`，和 base 一起导入。

    python scripts/build_supplement_handwritten.py
    python scripts/build_supplement_handwritten.py --check
"""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT.parent.parent / "deploy" / "seed" / "终末地世界书_通用版_v1.0.md"
BASE = ROOT / "data" / "rag_corpus" / "docs.jsonl"
OUT = ROOT / "data" / "rag_corpus" / "supplement_handwritten.jsonl"

_ENTRY_BLOCK = re.compile(r"\[W:ENTRY\][^\n]*\n+```[a-zA-Z]*\n(.*?)```", re.S)

# 手写这些条目的用途是"补语料里没有的规则/机制"。语料里的剧情条目是 mid，
# 同分时按标题排序，手写的会被剧情块挤掉 —— 所以整体抬一档。
_PRIORITY_UP = {"high": "high", "mid": "high", "low": "mid"}


def load_seed(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    match = _ENTRY_BLOCK.search(text)
    if not match:
        raise SystemExit(f"找不到 [W:ENTRY] 段：{path}")
    block = match.group(1)
    return json.loads(block[block.find("[") : block.rfind("]") + 1])


def base_text(path: Path) -> str:
    return "".join(json.loads(line)["text"] for line in path.open(encoding="utf-8") if line.strip())


def build(seed: list[dict], corpus: str) -> list[dict]:
    entries: list[dict] = []
    for item in seed:
        title = str(item.get("title") or "").strip()
        if not title or title in corpus:
            continue
        raw_body = item.get("body")
        body = (
            "\n\n".join(str(part).strip() for part in raw_body)
            if isinstance(raw_body, list)
            else str(raw_body or "").strip()
        )
        entries.append(
            {
                "id": "",
                "title": title,
                "kind": str(item.get("kind") or "term"),
                "priority": _PRIORITY_UP.get(str(item.get("priority") or "mid"), "high"),
                "keys": [str(key) for key in (item.get("keys") or [])],
                "fuzzy": [str(key) for key in (item.get("fuzzy") or [])],
                "semantic": str(item.get("semantic") or "")[:600],
                "body": body,
                "source": "handwritten",
                "doc_id": f"手写补充/{title}.md#0",
            }
        )
    for index, item in enumerate(entries, 1):
        item["id"] = f"h{index:04d}"
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="生成手写补充条目")
    parser.add_argument("--seed", default=str(SEED))
    parser.add_argument("--base", default=str(BASE))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    seed_path, base_path = Path(args.seed), Path(args.base)
    for path in (seed_path, base_path):
        if not path.exists():
            print(f"找不到文件：{path}")
            return 1
    entries = build(load_seed(seed_path), base_text(base_path))
    print(f"手写 {len(entries)} 条（base 语料里没有的标题）")
    for item in entries[:5]:
        print(f"  {item['id']} {item['title']} [{item['kind']}/{item['priority']}]")
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
