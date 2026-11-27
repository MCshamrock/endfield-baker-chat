"""scripts/import_prompts_v2.py —— 把老包的实验性长卡 `characters_v2/*.json` 导入数据库。

老包每个角色一个文件，长这样（三行就能看完）：

    {
      "id": "perlica",
      "name": "佩丽卡",
      "prompt": "### 一、角色身份\\n\\n你是【佩丽卡】……”   ← JSON 里的 \\n 只是转义写法
    }

读的时候必须用 `json.loads`：它会把 `\\n` 还原成**真实的换行符**。写库时直接写这个
字符串，**不 strip、不再转义** —— 老客户端读到的就是这些字节，多一个空格都算改人设。

三件必须记住的事：

1. **角色靠 `characters.slug` 对齐**。slug 就是文件名去掉 .json：
   `perlica.json` → slug = `perlica`。文件名和库里 slug 对不上就跳过并打印警告
   （宁可少导一个，也不要把提示词挂到错的人身上）。
2. 写进去的行是 `variant='v2'`、`active=1`。v1 那套一行都不碰。
3. **幂等**：按 (character_id, variant) 找已有行 —— 有就更新正文，没有才插入。
   重复跑不会多出第二套 v2，也不会撞唯一键 (character_id, variant, ver)。

用法（在 `web/back` 目录下）：

    .venv-linux/bin/python scripts/import_prompts_v2.py --dry-run   # 只看会做什么
    .venv-linux/bin/python scripts/import_prompts_v2.py             # 真写库

老包不在默认位置时，用 `--source` 指过去：

    .venv-linux/bin/python scripts/import_prompts_v2.py --source /path/to/characters_v2

连哪个库由 `BAKER_DATABASE_URL`（或 .env）决定，和别的脚本一致。
"""

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，直接 print 中文会变乱码。和 scripts/migrate.py 一样先修掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 允许 `python scripts/import_prompts_v2.py` 直接跑（此时 sys.path 里只有 scripts/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Character, CharacterPrompt  # noqa: E402

# 这套提示词在库里的变体标记。'v2' 是老客户端 use_new_prompt=True 时读的那套。
VARIANT = "v2"

# 老包的默认位置：
#   本文件在 <BakerAI>/BakerAI_new/web/back/scripts/import_prompts_v2.py
#   往上数：parents[0]=scripts, [1]=back, [2]=web, [3]=BakerAI_new, [4]=BakerAI
#   老包在 <BakerAI>/原始网站前后端/backend/characters/characters_v2
DEFAULT_SOURCE_DIR = (
    Path(__file__).resolve().parents[4]
    / "原始网站前后端"
    / "backend"
    / "characters"
    / "characters_v2"
)


def read_prompt_file(path: Path) -> tuple[str, str, str] | None:
    """读一个角色 json，返回 (slug, 显示名, 正文)；不是角色文件时返回 None。

    为什么 slug 用文件名而不是 json 里的 `id`：
    任务/老包约定就是"文件名 = slug"，而且 `characters.slug` 是唯一键；
    文件里的 id 只是冗余字段（万一哪天有人手改错了，以文件名为准更容易发现）。
    """
    if path.stem == "index":
        # index.json 是目录清单（数组），不是角色，必须跳过
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None

    body = data.get("prompt")
    if not isinstance(body, str) or not body:
        return None

    # ⚠️ 这里**绝对不能** body.strip()：正文首尾的换行也是老包的一部分。
    name = str(data.get("name") or path.stem)
    return path.stem, name, body


def import_all(
    db: Session, source_dir: Path = DEFAULT_SOURCE_DIR, *, dry_run: bool = False
) -> dict[str, int]:
    """把 source_dir 下所有角色 json 写进 character_prompts（variant='v2'）。

    返回统计数字（测试和命令行都用它核对）：
      files        真正处理的角色文件数
      inserted     新插入的 v2 行数
      updated      已有 v2 行被覆盖的条数
      skipped      不是角色文件的（index.json 之类）
      no_character 库里的 characters 表找不到这个 slug

    `dry_run=True` 时只统计、不写库（最后 rollback 一下，确保什么也没留下）。
    """
    stats = {"files": 0, "inserted": 0, "updated": 0, "skipped": 0, "no_character": 0}

    for path in sorted(source_dir.glob("*.json")):
        parsed = read_prompt_file(path)
        if parsed is None:
            print(f"跳过 {path.name}（不是角色文件）")
            stats["skipped"] += 1
            continue

        slug, name, body = parsed
        character = db.scalar(select(Character).where(Character.slug == slug))
        if character is None:
            print(f"跳过 {path.name}：characters 表里没有 slug={slug}")
            stats["no_character"] += 1
            continue

        # 幂等的关键：先按 (角色, 变体) 找行，不按文件名判断。
        # 找到了就更新（保留原来的 ver —— 版本号是"这套提示词的第几版"，不该被导入重置）。
        row = db.scalar(
            select(CharacterPrompt)
            .where(
                CharacterPrompt.character_id == character.id,
                CharacterPrompt.variant == VARIANT,
            )
            .order_by(CharacterPrompt.ver.desc())
            .limit(1)
        )

        stats["files"] += 1
        if row is None:
            stats["inserted"] += 1
            print(f"+ 新增 {slug}（{name}）{len(body)} 字")
            if not dry_run:
                db.add(
                    CharacterPrompt(
                        character_id=character.id,
                        ver=1,  # 两套变体各自从 ver=1 开始，唯一键里有 variant 所以不会撞
                        variant=VARIANT,
                        body=body,
                        active=1,
                        created_at=utcnow(),
                    )
                )
        else:
            stats["updated"] += 1
            print(f"~ 更新 {slug}（{name}）{len(body)} 字")
            if not dry_run:
                row.body = body
                row.active = 1

    if dry_run:
        # 试运行一行都不许落库。这里没有任何 add，rollback 只是双保险。
        db.rollback()
    else:
        db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 characters_v2/*.json 导入 character_prompts（variant=v2）"
    )
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE_DIR),
        help=f"characters_v2 目录，默认 {DEFAULT_SOURCE_DIR}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要做什么，不写库",
    )
    args = parser.parse_args()

    source_dir = Path(args.source)
    if not source_dir.is_dir():
        print(f"找不到目录：{source_dir}")
        return 1

    with SessionLocal() as db:
        stats = import_all(db, source_dir, dry_run=args.dry_run)

    prefix = "【试运行，未写库】" if args.dry_run else ""
    print(
        f"{prefix}处理 {stats['files']} 个角色文件："
        f"新增 {stats['inserted']}，更新 {stats['updated']}；"
        f"跳过 {stats['skipped']} 个非角色文件、{stats['no_character']} 个库里没有的角色"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
