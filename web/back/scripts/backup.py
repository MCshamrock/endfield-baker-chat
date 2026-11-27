"""scripts/backup.py —— 把「库 + 上传的图片 + 配置」打成一个可恢复的包。

为什么要有它：这个项目到现在**一份备份都没有**。库里的角色、世界书、47 把 key
和用户聊过的东西全在一台机器的硬盘上，盘坏了就没了。

包的结构（一个目录一件事，谁都能看懂）：

    baker-20261116-234012/
      manifest.json     清单（每张表行数、文件、sha256、工具版本）
      db.sql.gz         库的完整 dump（mysqldump --single-transaction）
      uploads.tar.gz    用户上传的图片（没有就跳过）
      .env              配置，含 BAKER_SECRET —— **没它 api_keys 解不开**

三条规矩：
  1. **先写临时目录，校验通过再改名**。备份到一半断掉，不会留下一个"看着像备份"的坏包。
  2. **跑完立刻回读校验**（sha256 + gzip 可解）。没验过的备份不算备份。
  3. **默认保留 14 份**，超了删最旧的。备份把磁盘塞满也是事故。

用法：
    .venv\\Scripts\\python.exe scripts\\backup.py
    .venv\\Scripts\\python.exe scripts\\backup.py --verify     # 恢复到临时库核对行数
    .venv\\Scripts\\python.exe scripts\\backup.py --out D:\\bak --keep 30
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.settings import get_settings  # noqa: E402

BACK_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACK_DIR.parents[1]  # <BakerAI_new>
DEFAULT_OUT = PROJECT_DIR / "_runtime" / "backups"


def find_mysqldump() -> str:
    """找 mysqldump：环境变量 > 便携版 MySQL > PATH。"""
    override = os.environ.get("BAKER_MYSQLDUMP", "")
    if override and Path(override).exists():
        return override

    runtime = PROJECT_DIR / "_runtime" / "mysql" / "env"
    for candidate in sorted(runtime.glob("mysql-*/bin/mysqldump*")):
        if candidate.suffix.lower() in ("", ".exe"):
            return str(candidate)

    found = shutil.which("mysqldump")
    if found:
        return found
    raise SystemExit("找不到 mysqldump。装 MySQL 客户端，或设 BAKER_MYSQLDUMP 指向它。")


def _client_binary(mysqldump: str, name: str) -> str:
    """和 mysqldump 同目录的 mysql 客户端（恢复时要用它）。"""
    sibling = Path(mysqldump).with_name(name + (".exe" if os.name == "nt" else ""))
    if not sibling.exists():
        raise SystemExit(f"找不到 {sibling.name}，恢复步骤需要它")
    return str(sibling)


def _write_cnf(url: URL) -> str:
    """把账号密码写进临时 cnf，避免出现在进程列表/命令历史里。"""
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False, encoding="utf-8") as handle:
        handle.write("[client]\n")
        handle.write(f"host={url.host or '127.0.0.1'}\n")
        handle.write(f"port={url.port or 3306}\n")
        handle.write(f"user={url.username or 'root'}\n")
        if url.password:
            handle.write(f'password="{url.password}"\n')
        return handle.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_counts(engine) -> dict[str, int]:
    """每张表多少行。恢复后拿它比对，比"文件大小看着差不多"靠谱得多。"""
    with engine.connect() as conn:
        names = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() ORDER BY table_name"
                )
            )
        ]
        return {
            name: int(conn.execute(text(f"SELECT COUNT(*) FROM `{name}`")).scalar() or 0)
            for name in names
        }


def dump_database(url: URL, dest: Path, mysqldump: str) -> None:
    """mysqldump → 明文 sql → gzip。

    为什么要分两步：`subprocess` 只认真实文件描述符，把 gzip 对象塞给 stdout
    它会绕过压缩直接写原始字节（文件名看着是 .gz，内容却是明文）—— 那个坑很隐蔽，
    只有回读校验才会发现，而那时候可能已经当成备份存了半年。
    """
    cnf_path = _write_cnf(url)
    plain = dest.with_suffix("")  # db.sql
    command = [
        mysqldump,
        f"--defaults-extra-file={cnf_path}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
        "--set-gtid-purged=OFF",
        "--skip-dump-date",
        str(url.database),
    ]
    try:
        with plain.open("wb") as handle:
            proc = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace")[:400]
            raise SystemExit(f"mysqldump 失败：{message}")
        with (
            plain.open("rb") as source,
            dest.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        ):
            shutil.copyfileobj(source, gz, length=1 << 20)
    finally:
        plain.unlink(missing_ok=True)
        Path(cnf_path).unlink(missing_ok=True)


def pack_uploads(upload_dir: Path, dest: Path) -> int:
    """把上传目录打成 tar.gz。返回文件数；没有文件就不建这个包。"""
    if not upload_dir.exists():
        return 0
    count = sum(1 for path in upload_dir.rglob("*") if path.is_file())
    if count == 0:
        return 0
    with tarfile.open(dest, "w:gz") as archive:
        archive.add(upload_dir, arcname="uploads")
    return count


def verify_archive(folder: Path, manifest: dict[str, Any]) -> None:
    """回读校验：文件在、大小对、sha256 对、gzip 能解。不合格直接抛。"""
    for item in manifest["files"]:
        path = folder / item["name"]
        if not path.exists():
            raise SystemExit(f"备份不完整：缺 {path.name}")
        if path.stat().st_size != item["bytes"]:
            raise SystemExit(f"{path.name} 大小不符")
        if _sha256(path) != item["sha256"]:
            raise SystemExit(f"{path.name} 校验和不符")
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as handle:
                if not handle.read(1024):
                    raise SystemExit(f"{path.name} 是个空压缩包")


def restore_and_compare(url: URL, dump: Path, mysqldump: str, expected: dict[str, int]) -> None:
    """把 dump 恢复进一个临时库，比对行数，再删掉。

    这是"备份能不能用"的唯一证明。只对文件大小的话，真出事时才发现坏就晚了。
    """
    scratch = f"{url.database}_verify"
    mysql = _client_binary(mysqldump, "mysql")
    cnf_path = _write_cnf(url)
    engine = create_engine(url)
    try:
        _ensure_scratch(engine, scratch)

        # 注意：gzip 对象不能直接当 subprocess 的 stdin —— 它的 fileno() 指向**压缩后的
        # 文件**，客户端会收到一堆二进制，报 "ASCII '0x00' appeared"。必须先解压成明文文件。
        plain = dump.parent / ".restore.tmp.sql"
        try:
            with gzip.open(dump, "rb") as source, plain.open("wb") as target:
                shutil.copyfileobj(source, target, length=1 << 20)
            with plain.open("rb") as handle:
                proc = subprocess.run(
                    [mysql, f"--defaults-extra-file={cnf_path}", "--default-character-set=utf8mb4", scratch],
                    stdin=handle,
                    capture_output=True,
                    check=False,
                )
        finally:
            plain.unlink(missing_ok=True)
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace")[:400]
            raise SystemExit(f"恢复失败：{message}")

        scratch_url = url.set(database=scratch)
        scratch_engine = create_engine(scratch_url)
        try:
            got = _table_counts(scratch_engine)
        finally:
            scratch_engine.dispose()

        wrong = {name: expected[name] for name in expected if got.get(name) != expected[name]}
        if wrong:
            raise SystemExit(f"恢复后行数不对：{wrong}")
        print(f"  恢复校验：{len(expected)} 张表行数全部一致")
    finally:
        engine.dispose()
        Path(cnf_path).unlink(missing_ok=True)


def _ensure_scratch(engine, scratch: str) -> None:
    """准备一个专门用来验证恢复的库。

    没权限建库时不报错：要求它事先存在（本地建一次就行，见 docs/05 §4）。
    生产账号通常只有业务库权限，逼它建库等于让校验永远跑不起来。
    """
    with engine.connect() as conn:
        try:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{scratch}` CHARACTER SET utf8mb4"))
            conn.commit()
        except OperationalError:
            conn.rollback()


def prune(out_dir: Path, keep: int) -> list[str]:
    """保留最近 keep 份，其余删掉。返回删掉的名字。"""
    folders = sorted(path for path in out_dir.glob("baker-*") if path.is_dir())
    removed: list[str] = []
    if keep <= 0:
        return removed
    for folder in folders[:-keep]:
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder.name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 BakerAI（库 + 图片 + 配置）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="备份放哪")
    parser.add_argument("--keep", type=int, default=14, help="保留几份（0 = 不删）")
    parser.add_argument("--verify", action="store_true", help="恢复到临时库核对行数")
    parser.add_argument("--no-env", action="store_true", help="不把 .env 打进包里")
    args = parser.parse_args()

    settings = get_settings()
    url = make_url(settings.database_url)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = out_dir / f"baker-{stamp}"
    work = out_dir / f".tmp-{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    print(f"备份到 {final}")
    engine = create_engine(settings.database_url)
    try:
        counts = _table_counts(engine)
        mysqldump = find_mysqldump()

        dump = work / "db.sql.gz"
        dump_database(url, dump, mysqldump)
        print(f"  库：{sum(counts.values())} 行 / {len(counts)} 张表，dump {dump.stat().st_size // 1024} KB")

        uploads = work / "uploads.tar.gz"
        upload_count = pack_uploads(Path(settings.upload_dir), uploads)
        if upload_count:
            print(f"  图片：{upload_count} 个文件，{uploads.stat().st_size // 1024} KB")

        env_file = BACK_DIR / ".env"
        if not args.no_env and env_file.exists():
            shutil.copy2(env_file, work / ".env")

        files = [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(work.iterdir())
        ]
        manifest: dict[str, Any] = {
            "created_at": stamp,
            "database": url.database,
            "tables": counts,
            "files": files,
            "mysqldump": mysqldump,
        }
        (work / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        verify_archive(work, manifest)
        print("  回读校验：文件齐全、sha256 与 gzip 都通过")

        if args.verify:
            restore_and_compare(url, dump, mysqldump, counts)

        work.rename(final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        engine.dispose()

    for name in prune(out_dir, args.keep):
        print(f"  清理旧备份：{name}")

    total = sum(item["bytes"] for item in files)
    print(f"完成：{final.name}（{total // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
