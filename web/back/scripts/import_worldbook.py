"""scripts/import_worldbook.py —— 把新式世界书 source.md 导入 worldbook 表。

用法：
    .venv\\Scripts\\python.exe scripts\\import_worldbook.py --file "<...>\\source.md" --world endfield

解析规则（新式世界书的格式）：
  在 `## [W:ENTRY] 世界书条目` 之后的第一段 ```json 围栏里，是一个数组；
  每条：{id, title, kind, priority, keys[], fuzzy[]?, semantic, body[]}。
  `body` 是段落数组 —— 拼成一段正文（段落之间空一行），因为拼进 prompt 时按一整段读。

幂等：按 (world, source_id) 覆盖写，重跑不会产生重复条目。
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import WorldbookEntry  # noqa: E402

# 匹配 `## [W:ENTRY] …` 之后、```json 围栏里的那张表。
# 注意：必须把**开头的围栏**也吃掉，否则捕获到的只是"围栏之前那一个换行"。
_ENTRY_BLOCK = re.compile(r"\[W:ENTRY\][^\n]*\n+```[a-zA-Z]*\n(.*?)```", re.S)


def parse_entries(text: str) -> list[dict[str, object]]:
    """从 markdown 里抠出条目数组。"""
    match = _ENTRY_BLOCK.search(text)
    if not match:
        raise ValueError("找不到 [W:ENTRY] 段的 ```json 围栏")
    block = match.group(1)
    start = block.find("[")
    end = block.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("[W:ENTRY] 段里没有 JSON 数组")
    data = json.loads(block[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("条目不是数组")
    return data


def body_text(raw: object) -> str:
    """body 可能是段落数组，也可能已经是一段字符串。"""
    if isinstance(raw, list):
        return "\n\n".join(str(part).strip() for part in raw if str(part).strip())
    return str(raw or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="导入世界书条目")
    parser.add_argument("--file", required=True)
    parser.add_argument("--world", default="endfield")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"找不到文件：{path}")
        return 1

    entries = parse_entries(path.read_text(encoding="utf-8"))
    print(f"解析到 {len(entries)} 条")

    created = 0
    updated = 0
    with SessionLocal() as db:
        for item in entries:
            source_id = str(item.get("id") or "").strip()
            if not source_id:
                continue
            row = db.scalar(
                select(WorldbookEntry).where(
                    WorldbookEntry.world_slug == args.world,
                    WorldbookEntry.source_id == source_id,
                )
            )
            raw_keys = item.get("keys")
            raw_fuzzy = item.get("fuzzy")
            keys = [str(key) for key in raw_keys] if isinstance(raw_keys, list) else []
            fuzzy = [str(key) for key in raw_fuzzy] if isinstance(raw_fuzzy, list) else []
            values = {
                "kind": str(item.get("kind") or "term"),
                "priority": str(item.get("priority") or "mid"),
                "title": str(item.get("title") or source_id),
                "semantic": str(item.get("semantic") or "")[:600],
                "keys": keys,
                "fuzzy": fuzzy,
                "body": body_text(item.get("body")),
                "is_constant": 1 if item.get("constant") else 0,
            }
            if row is None:
                db.add(
                    WorldbookEntry(
                        world_slug=args.world,
                        source_id=source_id,
                        updated_at=utcnow(),
                        **values,
                    )
                )
                created += 1
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                row.updated_at = utcnow()
                updated += 1
        db.commit()

    print(f"新建 {created} 条，更新 {updated} 条（world={args.world}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
