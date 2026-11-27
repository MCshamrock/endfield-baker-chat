#!/usr/bin/env bash
# backup_web.sh —— 只备份两块 web dist（A 预演 / B 正式），带 sha256 与文件数
#
# 产物：/opt/bakerai-backup/<时间戳>_web/
#   web-v2/          ← /var/www/peilika-v2 的完整副本（可直接 rsync 回去）
#   web-v2.tar.gz    ← 同一份的 tar（异地留存/审计）
#   web-chat/        ← /var/www/peilika-chat 的完整副本
#   web-chat.tar.gz
#   INDEX.txt        ← index.html 的 sha256、文件数、构建指纹（引用的 assets 文件名）
#
# 用法（root）：
#   bash backup_web.sh [--dest /opt/bakerai-backup] [--tag web]
#   --dry-run 只打印

SCRIPT_NAME=backup_web
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

DEST_ROOT=/opt/bakerai-backup
TAG=web

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST_ROOT="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

require_root
need_cmd rsync tar sha256sum find

TS="$(stamp)"
DIR="$DEST_ROOT/${TS}_${TAG}"
log "备份目录：$DIR"

declare -A SRC=( [web-v2]=/var/www/peilika-v2 [web-chat]=/var/www/peilika-chat )

[ "$DRY_RUN" = "0" ] && { mkdir -p "$DIR"; chmod 750 "$DEST_ROOT" "$DIR"; }
INDEX="$DIR/INDEX.txt"
[ "$DRY_RUN" = "0" ] && : >"$INDEX"

for name in web-v2 web-chat; do
    src="${SRC[$name]}"
    step "备份 $name ← $src"
    if [ ! -d "$src" ]; then warn "$src 不存在，跳过"; continue; fi

    # 文件数与 index.html 指纹（先算，便于事后核对"换的是不是这一份"）
    nfiles="$(find "$src" -type f | wc -l)"
    sha="$(sha_of "$src/index.html")"
    entry="$(grep -oE 'assets/[^"]+\.js' "$src/index.html" 2>/dev/null | head -1)"
    size="$(du -sh "$src" | cut -f1)"
    log "  $src: $nfiles 个文件 / $size / index.html sha256=${sha:0:16}… / 入口=${entry:-?}"

    if [ "$DRY_RUN" = "1" ]; then
        log "  \$ rsync -a --delete '$src/' '$DIR/$name/'"
        log "  \$ tar -czf '$DIR/$name.tar.gz' -C '$(dirname "$src")' '$(basename "$src")'"
    else
        mkdir -p "$DIR/$name"
        rsync -a --delete "$src/" "$DIR/$name/"
        tar -czf "$DIR/$name.tar.gz" -C "$(dirname "$src")" "$(basename "$src")"
        {
            echo "== $name =="
            echo "src=$src"
            echo "files=$nfiles"
            echo "size=$size"
            echo "index_sha256=$sha"
            echo "entry_js=$entry"
            echo "backup_dir=$DIR/$name"
            echo "backup_tar=$DIR/$name.tar.gz"
            echo "tar_sha256=$(sha_of "$DIR/$name.tar.gz")"
            echo
        } >>"$INDEX"
        ok "$name 备份完成：$DIR/$name（+ .tar.gz）"
    fi
done

if [ "$DRY_RUN" = "0" ]; then
    echo
    echo "  ---- INDEX.txt ----"
    sed 's/^/  /' "$INDEX"
    echo "  ---- 目录 ----"
    du -h "$DIR"/* | sort -h | sed 's/^/  /'
    printf '%s备份完成：%s%s\n' "$C_GRN" "$DIR" "$C_RST"
fi
