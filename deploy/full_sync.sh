#!/usr/bin/env bash
# full_sync.sh —— 后端**全量重传 + 全量哈希对账**（不做增量）
#
# 为什么全量：增量同步靠"我 diff 出来的 6 个文件"，一旦漏一个/传了旧的，线上就是
# 半新半旧，而且事后查不出来。全量传 + 逐文件 sha 对账，把"一致性"变成可判定的事。
#
# 流程（每一步失败就停，不带状态往下走）：
#   0) 预检：staging 包在、本地清单在、服务器 .env 在
#   1) 解包 staging → 建"新树"（只含 app/ schema/ scripts/ requirements.lock pyproject.toml）
#   2) 备份现有 back/ → back.bak_<ts>（硬链接副本，秒级、省空间）
#   3) rsync -a --delete 新树 → back/，**排除 .env / _runtime / .venv* / 日志 / 缓存**
#      （--delete 也尊重排除项，所以 .env 与上传目录不会被删）
#   4) 服务器侧生成清单 → 与本地清单逐文件 diff，必须 0 差异
#   5) restart + 六条复验
#
# 用法（root 不需要；用 y 跑；verification 里如果给了 admin 凭据会在第 5 段用）：
#   bash full_sync.sh --staged /tmp/bakerai-full-<ts> --local-manifest /tmp/manifest-local.txt
#   bash full_sync.sh --staged ... --local-manifest ... --skip-restart     # 只传+对账
#
# 参数：
#   --staged DIR           解包好的新树（含 app/ schema/ scripts/ requirements.lock …）
#   --local-manifest FILE  本地生成的清单（<sha>  <path>）
#   --dest DIR             目标（默认 /home/y/baker-v4/back）
#   --manifest-dir DIR     清单落盘目录（默认 /opt/bakerai-backup/manifests）
#   --skip-restart         不重启、不复验（只做同步+对账）
#   --dry-run              只打印
#
# 退出码：0 全绿；1 任一步失败（差异清单会打出来）

SCRIPT_NAME=full_sync
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
MANIFEST_PY="$HERE/lib/manifest.py"
# 对账白名单：两侧必须用同一份，否则服务器上的 .env/docs/tests 会被算成"只在服务器有"
ONLY_ARGS=(--only app --only schema --only scripts --only requirements.lock --only pyproject.toml)

STAGED=""
LOCAL_MANIFEST=""
DEST=/home/y/baker-v4/back
MANIFEST_DIR=/opt/bakerai-backup/manifests
SKIP_RESTART=0

while [ $# -gt 0 ]; do
    case "$1" in
        --staged) STAGED="${2:?}"; shift 2 ;;
        --local-manifest) LOCAL_MANIFEST="${2:?}"; shift 2 ;;
        --dest) DEST="${2:?}"; shift 2 ;;
        --manifest-dir) MANIFEST_DIR="${2:?}"; shift 2 ;;
        --skip-restart) SKIP_RESTART=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

need_cmd rsync python3 curl sha256sum
[ -n "$STAGED" ] || die "必须给 --staged <解包好的新树>"
[ -n "$LOCAL_MANIFEST" ] || die "必须给 --local-manifest <本地清单>"
[ -d "$STAGED" ] || die "staging 不存在：$STAGED"
[ -f "$LOCAL_MANIFEST" ] || die "本地清单不存在：$LOCAL_MANIFEST"
[ -x "$(command -v python3)" ] || die "缺 python3"
[ -f "$MANIFEST_PY" ] || die "缺 $MANIFEST_PY"
[ -d "$DEST" ] || die "目标目录不存在：$DEST"
[ -f "$DEST/.env" ] || warn "$DEST/.env 不存在（服务可能起不来，但同步继续）"

TS="$(stamp)"
BACKUP="$DEST.bak_$TS"
[ "$DRY_RUN" = "0" ] && { mkdir -p "$MANIFEST_DIR"; install -d -m 750 "$MANIFEST_DIR"; }

step "0. 预检"
for d in app schema scripts; do [ -d "$STAGED/$d" ] || die "staging 缺目录：$d"; done
[ -f "$STAGED/requirements.lock" ] || die "staging 缺 requirements.lock"
log "  源：$STAGED"
log "  目标：$DEST"
log "  本地清单：$LOCAL_MANIFEST（$(wc -l <"$LOCAL_MANIFEST") 条）"

step "1. 备份现网代码（硬链接副本，秒级）"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ cp -al $DEST $BACKUP"
else
    cp -al "$DEST" "$BACKUP" && ok "备份：$BACKUP"
fi

step "2. 覆盖（rsync -a --delete；排除 .env / _runtime / venv / 日志 / 缓存）"
# 关键：这些排除项同时作用于"传输"和"--delete"，
# 所以服务器上的 .env、上传目录（BAKER_UPLOAD_DIR 指向的 _runtime/uploads）不会被删。
RSYNC_OPTS=(-a --delete --human-readable
    --exclude='.env' --exclude='.env.*' --exclude='_runtime'
    --exclude='.venv' --exclude='.venv-*' --exclude='venv'
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.mypy_cache'
    --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='*.egg-info'
    --exclude='logs' --exclude='*.bak_*'
    --exclude='docs' --exclude='tests' --exclude='README.md' --exclude='.vscode' --exclude='.git')
run rsync "${RSYNC_OPTS[@]}" "$STAGED/" "$DEST/"

step "3. 全量对账（逐文件 sha256，必须 100% 一致）"
SRV_MANIFEST="$MANIFEST_DIR/${TS}-server.txt"
LOCAL_COPY="$MANIFEST_DIR/${TS}-local.txt"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ python3 manifest.py make $DEST $SRV_MANIFEST ${ONLY_ARGS[*]}"
    log "  \$ python3 manifest.py diff $LOCAL_COPY $SRV_MANIFEST"
else
    cp -a "$LOCAL_MANIFEST" "$LOCAL_COPY"
    python3 "$MANIFEST_PY" make "$DEST" "$SRV_MANIFEST" "${ONLY_ARGS[@]}"
    echo
    if ! python3 "$MANIFEST_PY" diff "$LOCAL_COPY" "$SRV_MANIFEST"; then
        warn "对账不一致 —— 不重启、不复验。差异见上（可回退：rsync -a --delete $BACKUP/ $DEST/）"
        die "全量对账失败"
    fi
    ok "对账通过：服务器与本地逐文件一致"
    log "  清单留档：$SRV_MANIFEST / $LOCAL_COPY"
fi

if [ "$SKIP_RESTART" = "1" ]; then
    warn "--skip-restart：不重启、不复验（服务仍是旧进程）"
    exit 0
fi

step "4. 重启"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ systemctl --user restart baker-api"
else
    systemctl --user restart baker-api
    for i in $(seq 1 30); do
        c=$(curl -sS -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:5660/health 2>/dev/null || echo 000)
        [ "$c" = "200" ] && break; sleep 1
    done
    [ "$c" = "200" ] || { journalctl --user -u baker-api -n 40 --no-pager; die "/health 没起来"; }
    ok "/health=200"
    systemctl --user show baker-api -p MainPID -p NRestarts -p ActiveEnterTimestamp | sed 's/^/  /'
fi

step "5. 复验（readyz / instances / smoke / characters.weight / admin 36 / 群 details）"
if [ "$DRY_RUN" = "1" ]; then
    log "  （dry-run）跳过功能复验"
    exit 0
fi
BASE=http://127.0.0.1:5660
echo -n "  /readyz: "; curl -sS -m 8 $BASE/readyz; echo
echo -n "  /instances keys: "; curl -sS -m 8 $BASE/instances | python3 -c 'import json,sys;print(json.load(sys.stdin).get("keys"))'
if [ -x "$HERE/smoke.sh" ] || [ -f "$HERE/smoke.sh" ]; then
    bash "$HERE/smoke.sh" --base "$BASE" --generate 2>&1 | grep -aE "合计：|失败项" | sed 's/^/  /'
fi
# admin 两项：凭据从 stdin 前两行读（不落盘、不进 argv），没有就 SKIP
if [ -f "$HERE/lib/seg1_verify_admin.py" ]; then
    python3 "$HERE/lib/seg1_verify_admin.py"
else
    warn "缺 lib/seg1_verify_admin.py，跳过 admin 两项（可用 /tmp/seg1_verify_admin.py 手跑）"
fi

printf '\n%s全量同步完成（清单 100%% 一致 + 复验见上）%s\n' "$C_GRN" "$C_RST"
echo "  回退整棵树：rsync -a --delete $BACKUP/ $DEST/ && systemctl --user restart baker-api"
