#!/usr/bin/env bash
# deploy_web.sh —— 换一份前端 dist 上去（A=test2 预演 / B=chat 正式），失败自动回滚上一份。
#
# 为什么单独一个脚本：换 dist 是**唯一不可逆**的一步（老文件被覆盖、没有 SW 可清），
# 所以它必须自带四件事：先备份 → 原子覆盖 → sha256 核对 → 打真实请求验收；不合格自己退回去。
#
# 用法（root）：
#   bash deploy_web.sh --host test2 --dist /tmp/newdist            # 预演站
#   bash deploy_web.sh --host prod  --dist /tmp/newdist            # 正式站（chat.peilika.beer）
#   bash deploy_web.sh --host staging --dist ... --dry-run         # 只打印
#
# 参数：
#   --host test2|staging|prod|chat   目标站点（test2/staging 同一个，prod/chat 同一个）
#   --dist DIR                       构建产物目录（必须含 index.html）
#   --root DIR                       覆盖站点根（默认按 host 取 /var/www/peilika-v2 或 -chat）
#   --backup-dir DIR                 备份根（默认 /opt/bakerai-backup）
#   --yes                            跳过确认
#   --dry-run                        只打印，不动文件
#
# 退出码：0 成功；1 任一验收失败（已自动回滚）

SCRIPT_NAME=deploy_web
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

HOST_KEY=""
DIST=""
ROOT_OVERRIDE=""
BACKUP_ROOT=/opt/bakerai-backup
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST_KEY="${2:?}"; shift 2 ;;
        --dist) DIST="${2:?}"; shift 2 ;;
        --root) ROOT_OVERRIDE="${2:?}"; shift 2 ;;
        --backup-dir) BACKUP_ROOT="${2:?}"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

require_root
need_cmd rsync curl sha256sum find

case "$HOST_KEY" in
    test2|staging|A) HOST_KEY=test2; WEB_ROOT=/var/www/peilika-v2;   SERVER_NAME=test2.peilika.beer; SITE=peilika-v2 ;;
    prod|chat|B)     HOST_KEY=prod;  WEB_ROOT=/var/www/peilika-chat; SERVER_NAME=chat.peilika.beer;  SITE=chat-peilika
                     # 保留旧素材：splash/ 启动层 + 三张运营图（删了只会断掉回旧包的路）
                     KEEP_MATERIAL=1 ;;
    *) die "--host 只能是 test2|prod（staging/chat 是别名）" ;;
esac
[ -n "$ROOT_OVERRIDE" ] && WEB_ROOT="$ROOT_OVERRIDE"
[ -n "$DIST" ] || die "必须给 --dist <构建产物目录>"
[ -d "$DIST" ] || die "dist 目录不存在：$DIST"
[ -f "$DIST/index.html" ] || die "$DIST/index.html 不存在，这不像一份前端构建产物"
[ -d "$WEB_ROOT" ] || die "站点根不存在：$WEB_ROOT"

step "0. 指纹（先算清楚"换的是哪一份"）"
DIST_SHA="$(sha_of "$DIST/index.html")"
DIST_ENTRY="$(grep -oE 'assets/[^"]+\.js' "$DIST/index.html" 2>/dev/null | head -1)"
DIST_FILES="$(find "$DIST" -type f | wc -l)"
CUR_SHA="$(sha_of "$WEB_ROOT/index.html")"
CUR_ENTRY="$(grep -oE 'assets/[^"]+\.js' "$WEB_ROOT/index.html" 2>/dev/null | head -1)"
CUR_FILES="$(find "$WEB_ROOT" -type f | wc -l)"
log "  新 dist : sha=${DIST_SHA:0:16}… 入口=${DIST_ENTRY:-?} 文件=$DIST_FILES"
log "  现 站点 : sha=${CUR_SHA:0:16}… 入口=${CUR_ENTRY:-?} 文件=$CUR_FILES"
[ "$DIST_SHA" = "$CUR_SHA" ] && warn "新 dist 与现站点 index.html 完全相同（没必要覆盖，但会照做）"
[ -n "$DIST_ENTRY" ] || die "新 dist 的 index.html 里找不到 assets/*.js —— 不像 Vite 构建产物"
log "  验收标记：$DIST_ENTRY（页面里出现它才算新版本生效）"

if [ "$ASSUME_YES" = "0" ] && [ "$DRY_RUN" = "0" ]; then
    warn "即将覆盖 $WEB_ROOT（$SERVER_NAME）"
    printf '  输入 yes 继续：'; read -r ans
    [ "$ans" = "yes" ] || die "已取消（什么都没动）"
fi

step "1. 备份现站点（失败时回滚用）"
TS="$(stamp)"
BK="$BACKUP_ROOT/${TS}_webrollback"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ rsync -a --delete $WEB_ROOT/ $BK/web-$HOST_KEY/"
else
    mkdir -p "$BK/web-$HOST_KEY"
    rsync -a --delete "$WEB_ROOT/" "$BK/web-$HOST_KEY/"
    {
        echo "host=$HOST_KEY"
        echo "root=$WEB_ROOT"
        echo "server_name=$SERVER_NAME"
        echo "files=$CUR_FILES"
        echo "index_sha256=$CUR_SHA"
        echo "entry_js=$CUR_ENTRY"
    } >"$BK/web-$HOST_KEY.INDEX.txt"
    ok "备份：$BK/web-$HOST_KEY（index.html sha=${CUR_SHA:0:16}…）"
fi

step "2. 覆盖（rsync -a --delete，排除 .well-known/）"
# 排除 .well-known/：那是证书验证/ACME 目录，删了会让续期失败（线上真实存在这个目录的可能性必须假设有）
RSYNC_EXCL=(--exclude='.well-known/')
if [ "${KEEP_MATERIAL:-0}" = "1" ]; then
    # 三张运营图在 dist 里是哈希文件名（如 qr_chat-Cad1KO0a.webp），必须用通配符
    RSYNC_EXCL+=(--exclude='splash/' --exclude='qr_chat*' --exclude='qr_notice*' --exclude='about_hero*')
fi
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ rsync -a --delete ${RSYNC_EXCL[*]} $DIST/ $WEB_ROOT/"
else
    rsync -a --delete "${RSYNC_EXCL[@]}" "$DIST/" "$WEB_ROOT/"
    chown -R www-data:www-data "$WEB_ROOT" 2>/dev/null || warn "chown 失败（可能不是 root）"
    ok "已覆盖 $WEB_ROOT"
fi

step "3. 核对（sha256 + 文件数）"
restore_and_die() {
    warn "$1 —— 立刻回滚到备份"
    if [ "$DRY_RUN" = "0" ] && [ -d "$BK/web-$HOST_KEY" ]; then
        rsync -a --delete "$BK/web-$HOST_KEY/" "$WEB_ROOT/"
        chown -R www-data:www-data "$WEB_ROOT" 2>/dev/null || true
        local now; now="$(sha_of "$WEB_ROOT/index.html")"
        [ "$now" = "$CUR_SHA" ] && ok "回滚完成：index.html 已恢复为 ${CUR_SHA:0:16}…" || warn "回滚后 sha 仍不对：$now"
    fi
    die "$1"
}
if [ "$DRY_RUN" = "0" ]; then
    NOW_SHA="$(sha_of "$WEB_ROOT/index.html")"
    [ "$NOW_SHA" = "$DIST_SHA" ] || restore_and_die "落地后 index.html sha256 不一致（$NOW_SHA ≠ $DIST_SHA）"
    ok "index.html sha256 = ${NOW_SHA:0:16}…（与 dist 一致）"
fi

step "4. 真实请求验收（带 Host 头打 127.0.0.1:80）"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ curl -H 'Host: $SERVER_NAME' http://127.0.0.1/  → 期望 200 且含 $DIST_ENTRY"
else
    CODE="$(curl -sS -m 15 -o "$HTTP_BODY_FILE" -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1/ || echo 000)"
    [ "$CODE" = "200" ] || restore_and_die "首页 HTTP $CODE（期望 200）"
    grep -qF "$DIST_ENTRY" "$HTTP_BODY_FILE" || restore_and_die "首页 200 但内容里没有 $DIST_ENTRY（还是旧页面/缓存）"
    ok "首页 200 且含新入口 $DIST_ENTRY"

    # 同源 API：网页要用 /v1/meta，404 说明该 vhost 还没插 include（切 nginx 之前就会这样）
    CODE="$(curl -sS -m 10 -o "$HTTP_BODY_FILE" -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1/v1/meta || echo 000)"
    if [ "$CODE" = "200" ] && grep -q '"api":"v1"' "$HTTP_BODY_FILE"; then
        ok "同源 /v1/meta 200"
    else
        warn "同源 /v1/meta 返回 $CODE —— 该 vhost 还没切后端（预期：先 deploy_web、再 switch_nginx），不算失败"
    fi
fi

step "5. 收尾"
if [ "$DRY_RUN" = "0" ]; then
    log "  备份保留在：$BK/web-$HOST_KEY"
    log "  回滚命令：bash rollback.sh --web-host $HOST_KEY --backup-root $BACKUP_ROOT"
    log "  站点文件数：$(find "$WEB_ROOT" -type f | wc -l)（原 $CUR_FILES）"
fi
if [ "$DRY_RUN" = "1" ]; then
    printf '\n%s(dry-run) 以上为将执行的动作；没有写文件、没有动站点%s\n' "$C_YEL" "$C_RST"
else
    printf '\n%sweb 部署完成：%s → %s%s\n' "$C_GRN" "$HOST_KEY" "$WEB_ROOT" "$C_RST"
fi
