#!/usr/bin/env bash
# full_sync_web.sh —— 前端 / 面板**整份 dist 重传 + 清单对账 + 验收**（不做增量）
#
# 适用三块（互不覆盖）：
#   test2  → /var/www/peilika-v2      预演站
#   prod   → /var/www/peilika-chat    正式站（chat.peilika.beer）
#   panel  → /var/www/panel           后台面板（新目录，永不覆盖别的站）
#
# 流程：解包 dist → 备份现站点 → rsync -a --delete（web 排除 .well-known/）→
#       生成并比对清单（index.html + assets/**）→ 带 Host 头验收 → 失败自动回滚
#
# 用法（root）：
#   bash full_sync_web.sh --host test2 --dist /tmp/newdist --local-manifest /tmp/web-manifest.txt
#   bash full_sync_web.sh --host panel --dist /tmp/paneldist --skip-http-check
#
# 参数：
#   --host test2|prod|panel       目标
#   --dist DIR                    解包好的 dist（必须含 index.html）
#   --root DIR                    覆盖站点根
#   --local-manifest FILE         本地清单（把 dist 传上去之前在本机生成）
#   --manifest-dir DIR            清单留档（默认 /opt/bakerai-backup/manifests）
#   --skip-http-check             不做 HTTP 验收（panel 首次装 vhost 前用）
#   --dry-run

SCRIPT_NAME=full_sync_web
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
MANIFEST_PY="$HERE/lib/manifest.py"

HOST_KEY=""
DIST=""
ROOT_OVERRIDE=""
LOCAL_MANIFEST=""
MANIFEST_DIR=/opt/bakerai-backup/manifests
SKIP_HTTP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST_KEY="${2:?}"; shift 2 ;;
        --dist) DIST="${2:?}"; shift 2 ;;
        --root) ROOT_OVERRIDE="${2:?}"; shift 2 ;;
        --local-manifest) LOCAL_MANIFEST="${2:?}"; shift 2 ;;
        --manifest-dir) MANIFEST_DIR="${2:?}"; shift 2 ;;
        --skip-http-check) SKIP_HTTP=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

require_root
need_cmd rsync python3 curl find sha256sum

case "$HOST_KEY" in
    test2|staging|A) HOST_KEY=test2; WEB_ROOT=/var/www/peilika-v2;   SERVER_NAME=test2.peilika.beer; EXCL=() ;;
    prod|chat|B)     HOST_KEY=prod;  WEB_ROOT=/var/www/peilika-chat; SERVER_NAME=chat.peilika.beer
                     # 2026-11-18 用户决定：chat 的旧素材（启动层 splash/ + 二维码/英雄图）保留。
                     # 新 dist 不引用它们，但删了只会断掉"回旧包"的可能，没有任何好处。
                     # 注意：这三张图在 dist 里是**带哈希的文件名**（qr_chat-Cad1KO0a.webp），
                     # 所以必须用通配符匹配，写死 qr_chat.webp 是匹配不到的（2026-11-18 实测验过）。
                     EXCL=(--exclude=.well-known/ --exclude=splash/
                           --exclude='qr_chat*' --exclude='qr_notice*' --exclude='about_hero*') ;;
    panel)           HOST_KEY=panel; WEB_ROOT=/var/www/panel;        SERVER_NAME=panel.peilika.beer; EXCL=() ;;
    *) die "--host 只能是 test2|prod|panel（staging/chat 是别名）" ;;
esac
[ -n "$ROOT_OVERRIDE" ] && WEB_ROOT="$ROOT_OVERRIDE"
[ -n "$DIST" ] || die "必须给 --dist <构建产物目录>"
[ -d "$DIST" ] || die "dist 不存在：$DIST"
[ -f "$DIST/index.html" ] || die "$DIST/index.html 不存在"
[ -n "$LOCAL_MANIFEST" ] || die "必须给 --local-manifest（本机对 $DIST 生成的清单）"
[ -f "$LOCAL_MANIFEST" ] || die "本地清单不存在：$LOCAL_MANIFEST"

TS="$(stamp)"
[ "$DRY_RUN" = "0" ] && install -d -m 750 "$MANIFEST_DIR"

step "0. 指纹"
DIST_ENTRY="$(grep -oE 'assets/[^"]+\.js' "$DIST/index.html" 2>/dev/null | head -1)"
log "  目标：$HOST_KEY → $WEB_ROOT（Host: $SERVER_NAME）"
log "  新 dist：$(find "$DIST" -type f | wc -l) 文件 / index sha=$(sha_of "$DIST/index.html" | cut -c1-16)… / 入口=${DIST_ENTRY:-?}"

step "1. 备份现站点"
BK="/opt/bakerai-backup/${TS}_webfull_$HOST_KEY"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ rsync -a --delete $WEB_ROOT/ $BK/"
else
    if [ -d "$WEB_ROOT" ]; then
        mkdir -p "$BK"
        rsync -a --delete "$WEB_ROOT/" "$BK/"
        ok "备份：$BK（index sha=$(sha_of "$WEB_ROOT/index.html" | cut -c1-16)…）"
    else
        warn "$WEB_ROOT 不存在（首次安装），跳过备份"
        mkdir -p "$WEB_ROOT"
    fi
fi

step "2. 整份覆盖（rsync -a --delete ${EXCL[*]:-无额外排除}）"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ rsync -a --delete ${EXCL[*]:-} $DIST/ $WEB_ROOT/"
else
    rsync -a --delete "${EXCL[@]}" "$DIST/" "$WEB_ROOT/"
    chown -R www-data:www-data "$WEB_ROOT" 2>/dev/null || warn "chown 失败"
    ok "已覆盖 $WEB_ROOT"
fi

step "3. 清单对账（index.html + assets/**，逐文件 sha256）"
SRV_MF="$MANIFEST_DIR/${TS}-${HOST_KEY}-server.txt"
LOC_MF="$MANIFEST_DIR/${TS}-${HOST_KEY}-local.txt"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ python3 manifest.py make $WEB_ROOT $SRV_MF && diff $LOC_MF $SRV_MF"
else
    cp -a "$LOCAL_MANIFEST" "$LOC_MF"
    python3 "$MANIFEST_PY" make "$WEB_ROOT" "$SRV_MF"
    if ! python3 "$MANIFEST_PY" diff "$LOC_MF" "$SRV_MF"; then
        warn "清单不一致 → 回滚"
        [ -d "$BK" ] && rsync -a --delete "$BK/" "$WEB_ROOT/" && chown -R www-data:www-data "$WEB_ROOT"
        die "清单对账失败"
    fi
    ok "清单 100% 一致（$(wc -l <"$SRV_MF") 条）"
fi

step "4. HTTP 验收"
if [ "$SKIP_HTTP" = "1" ] || [ "$DRY_RUN" = "1" ]; then
    warn "跳过 HTTP 验收（--skip-http-check / dry-run）"
else
    CODE="$(curl -sS -m 15 -o "$HTTP_BODY_FILE" -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1/ || echo 000)"
    if [ "$CODE" != "200" ] || ! grep -qF "$DIST_ENTRY" "$HTTP_BODY_FILE"; then
        warn "首页验收失败（HTTP $CODE / 入口未出现）→ 回滚"
        [ -d "$BK" ] && rsync -a --delete "$BK/" "$WEB_ROOT/" && chown -R www-data:www-data "$WEB_ROOT"
        die "HTTP 验收失败"
    fi
    ok "首页 200 且含 $DIST_ENTRY"
    CODE="$(curl -sS -m 10 -o "$HTTP_BODY_FILE" -w '%{http_code}' -H "Host: $SERVER_NAME" http://127.0.0.1/v1/meta || echo 000)"
    if [ "$CODE" = "200" ] && grep -q '"api":"v1"' "$HTTP_BODY_FILE"; then
        ok "同源 /v1/meta 200"
    else
        warn "同源 /v1/meta = $CODE（该 vhost 可能还没插 include，或 panel 还没加 vhost）"
    fi
fi

printf '\n%s%s 整份 dist 同步完成%s\n' "$C_GRN" "$HOST_KEY" "$C_RST"
echo "  回滚：rsync -a --delete $BK/ $WEB_ROOT/ && chown -R www-data:www-data $WEB_ROOT"
