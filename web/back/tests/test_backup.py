"""tests/test_backup.py —— 备份包必须是真的能用的包。

只守一件事，但这件事很致命：**文件名是 `.gz`，内容却是明文**。
`subprocess` 把 gzip 对象当 stdout 时会绕过压缩（`GzipFile.fileno()` 指向压缩文件本身），
得到的包看着像备份、其实解不开。只有回读才能发现 —— 所以这里回读。
"""

import gzip
import json
import subprocess
import sys
from pathlib import Path

BACK_DIR = Path(__file__).resolve().parents[1]


def test_backup_creates_verifiable_archive(tmp_path: Path) -> None:
    out = tmp_path / "backups"
    proc = subprocess.run(
        [
            sys.executable,
            str(BACK_DIR / "scripts" / "backup.py"),
            "--out",
            str(out),
            "--no-env",
            "--keep",
            "0",
        ],
        capture_output=True,
        text=True,
        cwd=str(BACK_DIR),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    folders = list(out.glob("baker-*"))
    assert len(folders) == 1
    folder = folders[0]

    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "characters" in manifest["tables"], "清单里应该有每张表的行数"
    assert manifest["created_at"]

    dump = folder / "db.sql.gz"
    assert dump.exists() and dump.stat().st_size > 0
    # 真解一次：必须是 SQL 文本，不是伪装成 .gz 的明文
    with gzip.open(dump, "rt", encoding="utf-8") as handle:
        head = handle.read(4000)
    assert "CREATE TABLE" in head
