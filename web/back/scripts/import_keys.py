"""scripts/import_keys.py —— 把老后端的 api_pool.json 导进 api_keys 表（加密存储）。

用法：
    .venv\\Scripts\\python.exe scripts\\import_keys.py --file "D:\\...\\api_pool.json"

三条规矩：
  1. **绝不在输出里打印 key**（连前缀都不打）。只报数量。
  2. 密码/密钥只从环境（`BAKER_SECRET`）来，不落盘、不进代码库。
  3. 幂等：按 label 覆盖写，重跑不会产生重复 key。

老文件的结构（2026-11-13 那版）：
    { "defaults": {base_url, model, rpm, ...},
      "apis": [ {"id","label","key","enabled", <可选 base_url / model / rpm>} ] }
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.keys import reset as reset_pool  # noqa: E402
from app.repos import api_keys as api_keys_repo  # noqa: E402
from app.settings import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="导入上游 key（加密后写库）")
    parser.add_argument("--file", required=True, help="api_pool.json 的路径")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"找不到文件：{path}")
        return 1

    settings = get_settings()
    if settings.secret == "dev-only-change-me":
        print("⚠ BAKER_SECRET 还是默认值。建议先在 .env 里换成随机串再导入，")
        print("  否则一旦库被打包，key 等于没加密（默认值是人人都知道的）。")

    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    apis = data.get("apis") or []

    imported = 0
    enabled_count = 0
    sites: dict[str, int] = {}

    with SessionLocal() as db:
        for item in apis:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            label = str(item.get("label") or item.get("id") or f"k{imported:02d}")
            base_url = str(item.get("base_url") or defaults.get("base_url") or settings.upstream_base_url)
            model = str(item.get("model") or defaults.get("model") or settings.upstream_model)
            rpm = int(item.get("rpm") or defaults.get("rpm") or settings.key_rpm_default)
            enabled = bool(item.get("enabled", True))

            api_keys_repo.upsert(
                db,
                label=label,
                key_enc=crypto.encrypt(key, settings.secret),
                base_url=base_url,
                model=model,
                rpm=rpm,
                enabled=enabled,
            )
            imported += 1
            enabled_count += 1 if enabled else 0
            sites[base_url] = sites.get(base_url, 0) + 1
        db.commit()

    reset_pool()
    print(f"导入 {imported} 个 key（其中 {enabled_count} 个启用），加密方式 AES-256-GCM")
    for site, count in sorted(sites.items()):
        print(f"  {site}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
