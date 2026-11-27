#!/usr/bin/env python3
"""生成 / 比对文件清单（sha256 + 相对路径），供全量重传对账用。

格式（每行一条，按路径字节序排序，LF 结尾，UTF-8）：
    <64 位 sha256><两个空格><相对路径>

为什么用 python 而不是 `find | sort` + `sha256sum`：
 * 两侧必须用**同一套**排序与路径分隔规则，否则 diff 会出现"假差异"；
 * 要能按同一份排除规则过滤（.venv* / __pycache__ / .env / 缓存目录）。

用法：
    python3 manifest.py make   <根目录> <输出文件> [--exclude a --exclude b]
    python3 manifest.py diff   <本地清单> <服务器清单>
"""
import hashlib
import os
import sys

DEFAULT_EXCLUDES = [
    ".venv", ".venv-linux", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".env", ".env.local", ".git", "node_modules",
]


def walk(root, excludes, only=None):
    ex = set(excludes)
    out = {}
    # only = 白名单（相对 root 的文件或目录）。全量对账必须两侧用同一份白名单，
    # 否则服务器上的 .env / docs / tests / .vscode 会被算成"只在服务器有"→ 假失败。
    if only:
        for rel in only:
            full = os.path.join(root, rel)
            if os.path.isfile(full):
                out[rel] = _sha(full)
            elif os.path.isdir(full):
                for dp, dn, fn in os.walk(full):
                    dn[:] = sorted(d for d in dn if d not in ex)
                    for name in sorted(fn):
                        if name in ex or name.endswith((".pyc", ".pyo")) or ".bak_" in name:
                            continue
                        f2 = os.path.join(dp, name)
                        rel2 = os.path.relpath(f2, root).replace(os.sep, "/")
                        out[rel2] = _sha(f2)
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ex)
        for name in sorted(filenames):
            if name in ex:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(part in ex for part in rel.split("/")):
                continue
            if name.endswith((".pyc", ".pyo")) or ".bak_" in name:
                continue
            out[rel] = _sha(full)
    return out


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_make(argv):
    root = argv[0]
    out_path = argv[1] if len(argv) > 1 else "-"
    excludes = list(DEFAULT_EXCLUDES)
    only = []
    rest = argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--exclude" and i + 1 < len(rest):
            excludes.append(rest[i + 1]); i += 2
        elif rest[i] == "--only" and i + 1 < len(rest):
            only.append(rest[i + 1]); i += 2
        else:
            i += 1
    manifest = walk(root, excludes, only or None)
    lines = [f"{manifest[p]}  {p}" for p in sorted(manifest)]
    text = "\n".join(lines) + "\n"
    if out_path == "-":
        sys.stdout.write(text)
    else:
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"{len(lines)} 条清单 → {out_path}")
    return 0


def cmd_diff(argv):
    a, b = argv[0], argv[1]

    def load(p):
        d = {}
        for line in open(p, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line:
                continue
            h, _, path = line.partition("  ")
            d[path] = h
        return d

    la, lb = load(a), load(b)
    only_a = sorted(p for p in la if p not in lb)
    only_b = sorted(p for p in lb if p not in la)
    differ = sorted(p for p in la if p in lb and la[p] != lb[p])
    print(f"本地 {len(la)} 条 / 服务器 {len(lb)} 条 / 相同 {len(set(la) & set(lb)) - len(differ)} 条")
    for title, items in (("只在本地（服务器缺）", only_a), ("只在服务器（本地缺）", only_b),
                         ("内容不同", differ)):
        print(f"\n== {title}: {len(items)} ==")
        for p in items[:60]:
            print(f"  {p}")
        if len(items) > 60:
            print(f"  … 还有 {len(items) - 60} 条")
    bad = len(only_a) + len(only_b) + len(differ)
    print(f"\n结论：{'✓ 100% 一致' if bad == 0 else f'✗ 有 {bad} 处不一致'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "make":
        raise SystemExit(cmd_make(args))
    if cmd == "diff":
        raise SystemExit(cmd_diff(args))
    print(f"未知子命令：{cmd}")
    raise SystemExit(2)
