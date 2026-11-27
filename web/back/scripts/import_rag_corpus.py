"""scripts/import_rag_corpus.py —— 把老后端的 RAG 语料导入 worldbook 表。

用法（在 back 目录下、用项目 venv）：
    .venv\\Scripts\\python.exe scripts\\import_rag_corpus.py --dry-run
    .venv\\Scripts\\python.exe scripts\\import_rag_corpus.py            # 只加/改，不删
    .venv\\Scripts\\python.exe scripts\\import_rag_corpus.py --prune    # 顺带删掉旧的手写条目

数据来源：`data/rag_corpus/docs.jsonl` —— 老后端向量库 `index-endfield-v2` 里那份
`docs.jsonl` 的原样拷贝（4408 块 / 547 个文件 / 约 84 万字）。换语料的办法是拿新的
docs.jsonl 覆盖这一个文件再重跑本脚本，**不要手改数据库**。

幂等：按 (world_slug, source_id) 覆盖写，重跑不会产生重复条目。
`--prune` 会删掉这个 world 下不在本次导入清单里的旧条目（比如上一版手写的 e001~e147）。

## 补充库（supplement_*.jsonl）

基本条目之外还挂着三份补充，和 base 一起导入、一起被 `--prune` 保护：

  supplement_prts.jsonl        PRTS 本地语料库里的终末地角色档案（官方原文，33 人 / 197 条）
  supplement_handwritten.jsonl 手写世界里 RAG 语料没有的那 23 条（规则/机制为主）
  supplement_preview.jsonl     2.0 前瞻里公开、但 PRTS（11-02 那份）还没有的内容

`--no-supplement` 可以只导 base（排查问题时用）。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import WorldbookEntry  # noqa: E402
from app.retrieval.corpus import (  # noqa: E402
    DEFAULT_MAX_DF_RATIO,
    DEFAULT_MAX_KEYS,
    build_entries,
    load_rows,
    summarize,
)

CORPUS_DIR = Path(__file__).resolve().parent.parent / "data" / "rag_corpus"
DEFAULT_DOCS = CORPUS_DIR / "docs.jsonl"


def load_supplement(directory: Path) -> list[dict]:
    """读 `supplement_*.jsonl`（和 base 同一个形状，多了 source / doc_id）。"""
    entries: list[dict] = []
    for path in sorted(directory.glob("supplement_*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    entries.append(json.loads(line))
    return entries


def emit(entries: list[dict], target: str) -> None:
    """把生成结果写成 jsonl，方便人工抽查（不进数据库）。"""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in entries:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"  已写出 {path}")


def upsert(entries: list[dict], world: str) -> tuple[int, int]:
    """按 source_id 覆盖写。返回 (新建, 更新)。"""
    created = 0
    updated = 0
    with SessionLocal() as db:
        existing = {
            row.source_id: row
            for row in db.scalars(
                select(WorldbookEntry).where(WorldbookEntry.world_slug == world)
            ).all()
        }
        for item in entries:
            values = {
                "kind": item["kind"],
                "priority": item["priority"],
                "title": item["title"],
                "semantic": item["semantic"],
                "keys": item["keys"],
                "fuzzy": item["fuzzy"],
                "body": item["body"],
                "is_constant": 0,
            }
            row = existing.get(item["id"])
            if row is None:
                db.add(
                    WorldbookEntry(
                        world_slug=world,
                        source_id=item["id"],
                        updated_at=utcnow(),
                        **values,
                    )
                )
                created += 1
                continue
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utcnow()
            updated += 1
        db.commit()
    return created, updated


def prune(entries: list[dict], world: str) -> int:
    """删掉本 world 下不在本次清单里的旧条目（换库时清掉手写那批）。"""
    keep = {item["id"] for item in entries}
    removed = 0
    with SessionLocal() as db:
        rows = db.scalars(select(WorldbookEntry).where(WorldbookEntry.world_slug == world)).all()
        for row in rows:
            if row.source_id in keep:
                continue
            db.delete(row)
            removed += 1
        db.commit()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="导入老 RAG 语料为世界书条目")
    parser.add_argument("--docs", default=str(DEFAULT_DOCS), help="老 RAG 的 docs.jsonl")
    parser.add_argument("--world", default="endfield", help="world_slug，默认 endfield")
    parser.add_argument(
        "--max-df", type=float, default=DEFAULT_MAX_DF_RATIO, help="触发词 df 上限比例"
    )
    parser.add_argument("--max-keys", type=int, default=DEFAULT_MAX_KEYS, help="每条最多几个触发词")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 块（调试用）")
    parser.add_argument("--emit", default="", help="顺便把生成结果写成 jsonl（便于人工检查）")
    parser.add_argument(
        "--prune", action="store_true", help="删掉本 world 下不在本次清单里的旧条目"
    )
    parser.add_argument(
        "--no-supplement", action="store_true", help="只导 base 语料，不导 supplement_*.jsonl"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    docs = Path(args.docs)
    if not docs.exists():
        print(f"找不到语料：{docs}")
        return 1

    rows = load_rows(str(docs))
    if args.limit:
        rows = rows[: args.limit]
    entries = build_entries(rows, max_df_ratio=args.max_df, max_keys=args.max_keys)
    if not entries:
        print("语料是空的，什么都没做")
        return 1
    base_count = len(entries)
    if not args.no_supplement:
        supplement = load_supplement(docs.parent)
        clashes = {item["id"] for item in entries} & {item["id"] for item in supplement}
        if clashes:
            print(f"补充库和 base 的编号撞了：{sorted(clashes)[:5]}")
            return 1
        entries = [*entries, *supplement]

    stats = summarize(entries)
    print(f"base {base_count} 条 + 补充 {len(entries) - base_count} 条")
    print(
        f"语料 {docs.name}：{len(rows)} 块 → 条目 {stats['entries']} 条"
        f"（{stats['titles']} 个标题 / {stats['chars']} 字 / 平均 {stats['avg_keys']} 个触发词）"
    )
    print(f"  kind     {stats['kinds']}")
    print(f"  priority {stats['priorities']}")
    print(f"  样例     {entries[0]['title']} → {entries[0]['keys']}")

    if args.emit:
        emit(entries, args.emit)
    if args.dry_run:
        print("--dry-run：没有写库")
        return 0

    created, updated = upsert(entries, args.world)
    removed = prune(entries, args.world) if args.prune else 0
    print(f"新建 {created} 条，更新 {updated} 条，删除 {removed} 条（world={args.world}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
