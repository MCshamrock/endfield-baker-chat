#!/usr/bin/env bash
# switch_nginx.sh —— 把 nginx 的流量切到新后端（或只打印将要改什么）。
#
# 它只做四件事：
#   1. 装 include 片段 /etc/nginx/snippets/bakerai-new-api.conf（反代 /v1 /chat /group /upload /fetch /instances …）
#   2. 把 api 系 vhost 的 upstream 从 5500/5810 换成 5660（**只改 upstream 那一行**）
#   3. 把网页 vhost（chat / test2）里插入一行 include（同源反代）+ client_max_body_size
#   4. nginx -t → reload → 用 Host 头从本机 80 端口逐条验证
#
# 为什么改「上游端口」而不是重写整个 vhost：
#   老 vhost 里的 SSE 配置、超时、keepalive 都是调过的；整体重写等于把这些经验丢掉。
#   换一行端口 = diff 一眼能看完，回滚也只是把那一行换回去。
#
# **旧 gateway 进程不杀**（用户明确要求）：切 nginx 只是把入口指到新后端；
#   老 perlica-* 31 个单元继续跑着，回滚时把 upstream 换回 5500 就 30 秒恢复。
#
# 用法（root）：
#   bash switch_nginx.sh --dry-run            # 只看 diff，不动任何文件
#   bash switch_nginx.sh --web both           # 正式(chat) + 预演(test2) 都切，api 也切
#   bash switch_nginx.sh --web staging        # 只切 test2（先小范围验证）
#   bash switch_nginx.sh --api no --web prod  # 只切网页同源，api 域名先不动
#
# 参数：
#   --web prod|staging|both|none   网页 vhost（默认 both）
#   --api yes|no                   api/api2 两个 vhost（默认 yes）
#   --state-dir DIR                备份与状态目录（默认 /opt/bakerai-backup/state）
#   --no-reload                    只写文件 + nginx -t，不 reload
#   --dry-run                      只打印 diff

SCRIPT_NAME=switch_nginx
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

WEB=both
API=yes
STATE_DIR=/opt/bakerai-backup/state
DO_RELOAD=1
SMOKE_OK=0
# nginx 配置根：正常是 /etc/nginx；本地演练时指到一份伪造副本（--nginx-root），
# 这样 --dry-run 的 diff 可以拿真配置结构来演，而不碰生产。
NGINX_ROOT=/etc/nginx

while [ $# -gt 0 ]; do
    case "$1" in
        --web) WEB="${2:?}"; shift 2 ;;
        --api) API="${2:?}"; shift 2 ;;
        --state-dir) STATE_DIR="${2:?}"; shift 2 ;;
        --nginx-root) NGINX_ROOT="${2:?}"; shift 2 ;;
        --no-reload) DO_RELOAD=0; shift ;;
        --smoke-ok) SMOKE_OK=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

require_root
need_cmd curl ss
command -v diff >/dev/null || die "缺 diff"
if command -v nginx >/dev/null 2>&1; then HAVE_NGINX=1; else HAVE_NGINX=0; fi
[ "$HAVE_NGINX" = "1" ] || { [ "$DRY_RUN" = "1" ] && warn "本机没有 nginx 二进制（dry-run 只做 diff，不做语法校验）" || die "没有 nginx 二进制"; }

# nginx -t：正常路径就是 nginx -t；演练（--nginx-root 指到伪造副本）时用 -c/-p 指过去，
# 这样语法校验是真的，但读的是副本、不碰生产配置。
nginx_test() {
    # 用 "输出里有没有 syntax is ok" 判定，而不是退出码：
    # 非 root 跑 -t 时 nginx 会因为 bind :80 权限不足而返回非 0，
    # 但配置本身是合法的 —— 这种情况不该拦住切换（真正的 reload 用 root 做）。
    local out=""
    if [ "$NGINX_ROOT" = "/etc/nginx" ]; then
        out="$(nginx -t 2>&1 || true)"
    else
        out="$(nginx -t -c "$NGINX_ROOT/nginx.conf" -p "$NGINX_ROOT/" 2>&1 || true)"
        out="$(printf '%s' "$out" | sed "s#$NGINX_ROOT#<root>#g")"
    fi
    printf '%s\n' "$out"
    printf '%s' "$out" | grep -q 'syntax is ok'
}

SNIPPET=$NGINX_ROOT/snippets/bakerai-new-api.conf
SNIPPET_SRC="$HERE/nginx/bakerai-new-api.conf"
# 网页 vhost 专用补充片段：/group/ 这类 api 系 vhost 本来就有的 location，
# 放进共享片段会 duplicate location（2026-11-18 01:36 实测踩过）。
WEB_SNIPPET=$NGINX_ROOT/snippets/bakerai-web-extra.conf
WEB_SNIPPET_SRC="$HERE/nginx/bakerai-web-extra.conf"
AVAIL=$NGINX_ROOT/sites-available
NEW_PORT=5660

API_FILES=("$AVAIL/api-peilika" "$AVAIL/api2-peilika")
declare -A WEB_FILES=(
    [prod]="$AVAIL/chat-peilika"     # chat.peilika.beer → /var/www/peilika-chat（正式）
    [staging]="$AVAIL/peilika-v2"    # test2.peilika.beer → /var/www/peilika-v2（预演）
)

TS="$(stamp)"
MODIFIED=()
BACKUPS=()

[ -f "$SNIPPET_SRC" ] || die "模板不存在：$SNIPPET_SRC"

step "0. 前置检查"
port_listening "$NEW_PORT" && ok "5660 在听（新后端已起）" || warn "5660 没在听 —— 切过去会 502，先确认 deploy_backend.sh 跑过"
if [ "$HAVE_NGINX" = "0" ]; then
    warn "跳过基线 nginx -t（本机没有 nginx）"
elif nginx_test 2>&1 | grep -q 'syntax is ok'; then
    ok "nginx 当前配置合法（基线正常）"
else
    # 非 root 跑 dry-run 时，nginx -t 会因为读不了 600 的 vhost 而失败 —— 那不是配置的错
    if [ "$DRY_RUN" = "1" ] && ! is_root; then
        warn "非 root 无法校验基线（vhost 里有 600 文件），跳过；真跑请用 root"
    else
        die "nginx 基线配置就不合法，先修好再切"
    fi
fi
log "state 目录：$STATE_DIR"

# 备份一个文件并登记（同一个文件只备一次）
backup_once() {
    local f="$1" bak="$1.bak_bakerai_$TS"
    if [ ! -f "$f" ]; then warn "要改的文件不存在：$f（跳过）"; return 1; fi
    if [ "$DRY_RUN" = "0" ]; then
        cp -a "$f" "$bak"
        BACKUPS+=("$bak")
    fi
    MODIFIED+=("$f")
    return 0
}

diff_or_apply() {
    # $1 = 目标文件, $2 = 新内容临时文件, $3 = 说明
    local target="$1" newf="$2" what="$3"
    if diff -q "$target" "$newf" >/dev/null 2>&1; then
        ok "$what：无变化（已经切过，幂等跳过）"
        return 0
    fi
    echo
    echo "  ---- diff: $target ----"
    diff -u "$target" "$newf" | sed 's/^/  /' || true
    if [ "$DRY_RUN" = "0" ]; then
        cp "$newf" "$target"
        ok "$what：已写入"
    else
        log "(dry-run) 未写入"
    fi
}

# ---------------------------------------------------------------- 1. 片段
step "0.5 红线门禁（不满足就不许切 —— 这是唯一判据）"
# 直接问 5660（不经过 nginx），确认新后端真的可用；
# 任何一项不对就 die —— 此时还没改任何配置，回滚成本为零。
GATE_FAIL=0
gate_get() { curl -sS -m 8 "http://127.0.0.1:${NEW_PORT}$1" 2>/dev/null; }
H_BODY="$(gate_get /health)"
R_BODY="$(gate_get /readyz)"
I_BODY="$(gate_get /instances)"
jsonf() { printf '%s' "$1" | python3 -c "import json,sys;d=json.load(sys.stdin);print($2)" 2>/dev/null || echo ""; }

case "$H_BODY" in
    *baker-backend*) ok "/health → baker-backend" ;;
    *) warn "/health 不对（新后端没起？）：${H_BODY:0:100}"; GATE_FAIL=1 ;;
esac
MIG="$(jsonf "$R_BODY" 'd.get("migrations","")')"
if [ "$MIG" = "019_prompt_variant" ]; then ok "/readyz migrations=019_prompt_variant"
else warn "/readyz migrations=$MIG（要求 019_prompt_variant）"; GATE_FAIL=1; fi
KT="$(jsonf "$I_BODY" 'd.get("keys",{}).get("total")')"
KE="$(jsonf "$I_BODY" 'd.get("keys",{}).get("enabled")')"
if [ "$KT" = "47" ] && [ "$KE" = "25" ]; then ok "/instances keys.total=$KT enabled=$KE"
else warn "/instances keys.total=$KT enabled=$KE（要求 47 / 25）"; GATE_FAIL=1; fi

# smoke --generate 的证据：要求运维在跑本脚本前手动确认过（脚本没法替你调上游）
if [ "${SKIP_SMOKE_PROOF:-0}" != "1" ]; then
    warn "门禁提醒：必须先跑过 smoke.sh --generate 且 /chat 的 reply 非空（脚本无法替你验证"能出话"）"
    warn "  已确认就重跑并加 --smoke-ok；或在环境变量里 SKIP_SMOKE_PROOF=1"
    if [ "$SMOKE_OK" != "1" ]; then
        die "未确认 smoke --generate（--smoke-ok 表示已确认）。不切。"
    fi
    ok "已确认 smoke --generate 出过话（--smoke-ok）"
fi

[ "$GATE_FAIL" -eq 0 ] || die "红线门禁未通过：不切 nginx（老 gateway 继续服务）"
ok "门禁通过：允许切换"

step "1. 安装 include 片段 → $SNIPPET"
if [ "$DRY_RUN" = "0" ]; then
    install -d -m 755 "$NGINX_ROOT/snippets"
    install -m 644 "$SNIPPET_SRC" "$SNIPPET"
    [ -f "$WEB_SNIPPET_SRC" ] && install -m 644 "$WEB_SNIPPET_SRC" "$WEB_SNIPPET"
    ok "片段已安装（$(wc -l <"$SNIPPET") 行）"
else
    log "  \$ install -m 644 $SNIPPET_SRC $SNIPPET"
fi

# ---------------------------------------------------------------- 2. api vhost
if [ "$API" = "yes" ]; then
    step "2. api 系 vhost：upstream 5500/5810 → $NEW_PORT，并插入 include"
    for f in "${API_FILES[@]}"; do
        [ -f "$f" ] || { warn "不存在：$f"; continue; }
        TMP="$(mktemp)"; cp "$f" "$TMP"
        sed -i -E "s|^([[:space:]]*server)[[:space:]]+127\.0\.0\.1:5500;|\1 127.0.0.1:$NEW_PORT;|" "$TMP"
        sed -i -E "s|^([[:space:]]*server)[[:space:]]+127\.0\.0\.1:5810;|\1 127.0.0.1:$NEW_PORT;|" "$TMP"
        # 插入 include + 上传上限（在 server_name 之后，只插一次）
        if ! grep -q 'bakerai-new-api.conf' "$TMP"; then
            sed -i -E "s|^([[:space:]]*server_name[[:space:]]+[^;]+;)|\1\n\n    client_max_body_size 64m;\n    include $SNIPPET;|" "$TMP"
        fi
        backup_once "$f" && diff_or_apply "$f" "$TMP" "$(basename "$f")"
        rm -f "$TMP"
    done
else
    warn "--api no：api/api2 两个域名继续走老 gateway（老客户端不受影响）"
fi

# ---------------------------------------------------------------- 3. web vhost
case "$WEB" in
    both) WEB_LIST=(prod staging) ;;
    prod) WEB_LIST=(prod) ;;
    staging) WEB_LIST=(staging) ;;
    none) WEB_LIST=() ;;
    *) die "--web 只能是 prod|staging|both|none" ;;
esac
if [ "${#WEB_LIST[@]}" -gt 0 ]; then
    step "3. 网页 vhost 同源反代：${WEB_LIST[*]}"
    for k in "${WEB_LIST[@]}"; do
        f="${WEB_FILES[$k]}"
        [ -f "$f" ] || { warn "不存在：$f"; continue; }
        TMP="$(mktemp)"; cp "$f" "$TMP"
        if ! grep -q 'bakerai-new-api.conf' "$TMP"; then
            # 注释里含 #，所以 sed 用 | 当分隔符
            sed -i -E "s|^([[:space:]]*server_name[[:space:]]+[^;]+;)|\1\n\n    # 与新后端同源：/v1/* /group/* /chat /instances /upload /fetch 全部反代到 5660\n    client_max_body_size 64m;\n    include $SNIPPET;\n    include $WEB_SNIPPET;|" "$TMP"
        fi
        backup_once "$f" && diff_or_apply "$f" "$TMP" "$(basename "$f")"
        rm -f "$TMP"
    done
else
    warn "--web none：网页 vhost 不动"
fi

# ---------------------------------------------------------------- 4. 校验 + reload
step "4. nginx -t（不合法就整体回滚，绝不让坏配置生效）"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ nginx -t   （dry-run 不写文件，这里只检查当前配置）"
    if [ "$HAVE_NGINX" = "1" ]; then nginx_test 2>&1 | sed 's/^/  /'; else warn "本机没有 nginx，跳过"; fi
    echo
    log "(dry-run) 未 reload。去掉 --dry-run 才会真的生效。"
    exit 0
fi

if ! nginx_test 2>&1 | sed 's/^/  /'; then
    warn "nginx -t 失败 → 立刻还原本次改过的文件"
    for bak in "${BACKUPS[@]}"; do
        orig="${bak%%.bak_bakerai_*}"
        cp -a "$bak" "$orig" && warn "已还原 $orig"
    done
    die "配置不合法，什么都没生效。看上面的报错行号"
fi
ok "nginx -t 通过"

if [ "$DO_RELOAD" = "1" ]; then
    systemctl reload nginx && ok "nginx reload 完成" || die "reload 失败"
else
    warn "--no-reload：文件已写入，reload 后生效"
fi

# ---------------------------------------------------------------- 5. 写状态文件（rollback.sh 靠它）
step "5. 记录状态（回滚用）"
if [ "$NGINX_ROOT" != "/etc/nginx" ]; then
    warn "演练模式（--nginx-root $NGINX_ROOT）：不写状态文件、不做端口验证"
    echo
    log "副本配置已校验通过；真配置未动。"
    exit 0
fi
install -d -m 750 "$STATE_DIR"
STATE_FILE="$STATE_DIR/nginx-last.txt"
{
    echo "ts=$TS"
    echo "web=$WEB"
    echo "api=$API"
    echo "snippet=$SNIPPET"
    for f in "${MODIFIED[@]}"; do echo "file=$f"; done
    for b in "${BACKUPS[@]}"; do echo "backup=$b"; done
} >"$STATE_FILE"
ok "状态写入 $STATE_FILE"

# ---------------------------------------------------------------- 6. 从本机 80 端口验证（带 Host 头，绕过 CF）
step "6. 验证（127.0.0.1:80 + Host 头）"
FAIL=0
check() {
    local host="$1" path="$2" want="$3" note="$4" code
    # reload 之后老 worker 还会活一小会儿（优雅退出），刚 reload 完立刻查可能拿到旧结果。
    # 2026-11-18 01:36 实测：/v1/meta 在 reload 后 1 秒内返回 404，重试一次就是 200。
    local try
    for try in 1 2 3 4 5; do
        code="$(http_code -H "Host: $host" "http://127.0.0.1$path")"
        [ "$code" = "$want" ] && break
        sleep 1
    done
    if [ "$code" = "$want" ]; then
        printf '  %s✓%s %-22s %-14s → %s  %s\n' "$C_GRN" "$C_RST" "$host" "$path" "$code" "$note"
    else
        printf '  %s✗%s %-22s %-14s → %s (期望 %s)  %s\n' "$C_RED" "$C_RST" "$host" "$path" "$code" "$want" "$note"
        FAIL=$((FAIL + 1))
    fi
}

if [ "$API" = "yes" ]; then
    check api.peilika.beer  /health     200 "老监控读这个（现在应该回 baker-backend）"
    check api.peilika.beer  /instances  200 "keys.total 应为 47"
    check api2.peilika.beer /health     200 "备用域名同上游"
    check api.peilika.beer  /v1/meta    200 "新契约"
fi
for k in "${WEB_LIST[@]}"; do
    case "$k" in
        prod) h=chat.peilika.beer ;;
        staging) h=test2.peilika.beer ;;
    esac
    check "$h" /          200 "静态首页"
    check "$h" /v1/meta   200 "同源 API"
done

# /health 的 body 要能区分新旧后端：新的是 baker-backend，老的是 gateway_service
if [ "$API" = "yes" ]; then
    BODY="$(curl -sS -m 10 -H 'Host: api.peilika.beer' http://127.0.0.1/health 2>/dev/null || true)"
    echo
    echo "  api.peilika.beer/health → $BODY"
    case "$BODY" in
        *baker-backend*) ok "确认已在跑新后端" ;;
        *gateway_service*) warn "还是老网关！upstream 没换成功，检查 $AVAIL/api-peilika" ;;
        *) warn "响应形状不认识：$BODY" ;;
    esac
fi

[ "$FAIL" -eq 0 ] || die "有 $FAIL 项验证失败（配置已生效，可按 rollback.sh 回滚）"
printf '\n%snginx 切换完成且验证通过%s\n' "$C_GRN" "$C_RST"
echo "回滚： rollback.sh --backup-ts $TS"
