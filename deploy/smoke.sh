#!/usr/bin/env bash
# smoke.sh —— 上线冒烟：一次把"新后端到底行不行"全测一遍，任一失败非 0 退出。
#
# 设计取舍：
#   * 默认**不打上游**（不花钱、不占额度）：/chat 这条只验"路由通 + 错误形状对"，
#     真出话要显式加 --generate。
#   * 每条都给"成功判据"，不只打 HTTP 码 —— 200 但 body 里 ok:false 也算失败。
#   * 只用 python3 解析 JSON，不依赖 jq（服务器没装 jq）。
#
# 用法：
#   bash smoke.sh                                   # 直连 127.0.0.1:5660
#   bash smoke.sh --base http://127.0.0.1 --host api.peilika.beer   # 走 nginx（带 Host 头）
#   bash smoke.sh --web https://chat.peilika.beer   # 顺带测网页同源
#   bash smoke.sh --base https://api.peilika.beer --generate         # 公网 + 真出话
#
# 参数：
#   --base URL      后端地址（默认 http://127.0.0.1:5660）
#   --host HOST     走 nginx 时用它打 127.0.0.1（会覆盖 --base 的 host，端口取 --base 的）
#   --web URL       网页地址（测静态首页 + 同源 /v1/meta）
#   --expect-keys N key 总数（默认 47）
#   --expect-chars N 角色数（默认 36）
#   --generate      真的调一次上游（消耗一次 key 额度，约 1 秒）
#   --quiet         只打结果表

SCRIPT_NAME=smoke
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

BASE=http://127.0.0.1:5660
HOST_HDR=""
WEB_URL=""
EXPECT_KEYS=47
EXPECT_CHARS=36
DO_GENERATE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE="${2:?}"; shift 2 ;;
        --host) HOST_HDR="${2:?}"; shift 2 ;;
        --web) WEB_URL="${2:?}"; shift 2 ;;
        --expect-keys) EXPECT_KEYS="${2:?}"; shift 2 ;;
        --expect-chars) EXPECT_CHARS="${2:?}"; shift 2 ;;
        --generate) DO_GENERATE=1; shift ;;
        --quiet) shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

need_cmd curl python3

PASS=0; FAIL=0; SKIP=0
declare -a ROWS=()

record() { # $1=状态(PASS/FAIL/SKIP) $2=名字 $3=证据
    local st="$1" name="$2" ev="$3"
    case "$st" in
        PASS) PASS=$((PASS+1)); mark="${C_GRN}✓ PASS${C_RST}" ;;
        FAIL) FAIL=$((FAIL+1)); mark="${C_RED}✗ FAIL${C_RST}" ;;
        *)    SKIP=$((SKIP+1)); mark="${C_YEL}- SKIP${C_RST}" ;;
    esac
    printf '  %s  %-42s %s\n' "$mark" "$name" "$ev"
    ROWS+=("$st|$name|$ev")
}

CURL_COMMON=(-sS -m "${HTTP_TIMEOUT:-30}")
if [ -n "$HOST_HDR" ]; then CURL_COMMON+=(-H "Host: $HOST_HDR"); fi

req() { # req METHOD PATH [json] [extra-header] → 设 CODE / BODY
    local method="$1" path="$2" data="${3:-}" hdr="${4:-}"
    local args=("${CURL_COMMON[@]}" -X "$method" -o "$HTTP_BODY_FILE" -w '%{http_code}' -H 'Content-Type: application/json')
    [ -n "$hdr" ] && args+=(-H "$hdr")
    if [ -n "$data" ]; then args+=(--data-binary "$data"); fi
    CODE="$(curl "${args[@]}" "$BASE$path" 2>/dev/null || echo 000)"
    BODY="$(http_body)"
}

echo "=============================================================="
echo " BakerAI 冒烟测试"
echo " 后端：$BASE ${HOST_HDR:+(Host: $HOST_HDR)}"
echo " 网页：${WEB_URL:-（未指定，跳过静态检查）}"
echo " 时间：$(date -Is)"
echo "=============================================================="

# ---------------------------------------------------------------- 1. /health
req GET /health
if [ "$CODE" = "200" ] && printf '%s' "$BODY" | grep -q '"status":"ok"'; then
    record PASS "/health 进程活着" "200 $(printf '%s' "$BODY" | head -c 80)"
else
    record FAIL "/health" "HTTP $CODE $(printf '%s' "$BODY" | head -c 120)"
fi

# ---------------------------------------------------------------- 2. /readyz
req GET /readyz
MIG="$(json_get 'd.get("migrations","")' || true)"
if [ "$CODE" = "200" ] && [ "$(json_get 'd.get("ok")' || true)" = "True" ]; then
    if [ "${MIG%%_*}" = "019" ]; then
        record PASS "/readyz 库通 + 迁移到 019" "200 migrations=$MIG"
    else
        record FAIL "/readyz 迁移版本不对" "200 migrations=$MIG（期望 019_*）"
    fi
else
    record FAIL "/readyz 库/迁移有问题" "HTTP $CODE $(printf '%s' "$BODY" | head -c 120)"
fi

# ---------------------------------------------------------------- 3. /instances（老运维/老客户端的形状）
req GET /instances
K_TOTAL="$(json_get 'd.get("keys",{}).get("total")' || true)"
K_EN="$(json_get 'd.get("keys",{}).get("enabled")' || true)"
MODE="$(json_get 'd.get("mode","")' || true)"
CHAT_ST="$(json_get 'd.get("modules",{}).get("chat",[{}])[0].get("status","")' || true)"
if [ "$CODE" = "200" ] && [ "${K_TOTAL:-0}" -eq "$EXPECT_KEYS" ]; then
    record PASS "/instances keys.total=$EXPECT_KEYS" "enabled=$K_EN mode=$MODE chat=$CHAT_ST"
else
    record FAIL "/instances keys 数量不对" "HTTP $CODE total=${K_TOTAL:-?}（期望 $EXPECT_KEYS）"
fi

# ---------------------------------------------------------------- 4. /v1/meta
req GET /v1/meta
CAPS="$(json_get '",".join(d.get("caps",[]))' || true)"
WEBSEARCH="$(json_get 'd.get("capabilities",{}).get("web_search")' || true)"
if [ "$CODE" = "200" ]; then
    missing=""
    for c in group login memory continue; do
        case ",$CAPS," in *",$c,"*) ;; *) missing="$missing $c" ;; esac
    done
    if [ -z "$missing" ] && [ "$WEBSEARCH" = "False" ]; then
        record PASS "/v1/meta 能力声明齐全" "caps=$(printf '%s' "$CAPS" | cut -c1-70)…"
    else
        record FAIL "/v1/meta 能力缺失" "缺:${missing:-无} web_search=$WEBSEARCH"
    fi
else
    record FAIL "/v1/meta" "HTTP $CODE $(printf '%s' "$BODY" | head -c 120)"
fi

# ---------------------------------------------------------------- 5. 设备注册（拿 token，后面几条要用）
req POST /v1/auth/device '{"platform":"smoke","app_version":"3.2.2"}'
TOKEN="$(json_get 'd.get("token","")' || true)"
if [ "$CODE" = "201" ] && [ -n "$TOKEN" ]; then
    record PASS "/v1/auth/device 注册设备" "201 token=${TOKEN:0:6}… len=${#TOKEN}"
else
    record FAIL "/v1/auth/device" "HTTP $CODE $(printf '%s' "$BODY" | head -c 160)"
fi
AUTH_HDR=""
[ -n "$TOKEN" ] && AUTH_HDR="Authorization: Bearer $TOKEN"

# ---------------------------------------------------------------- 6. 角色表（老客户端靠它画卡墙）
if [ -n "$AUTH_HDR" ]; then
    req GET /v1/characters "" "$AUTH_HDR"
    N_CH="$(json_get 'len(d.get("items") or d.get("characters") or [])' || true)"
    if [ "$CODE" = "200" ] && [ "${N_CH:-0}" -ge "$EXPECT_CHARS" ]; then
        record PASS "/v1/characters 角色数" "count=$N_CH（期望 ≥$EXPECT_CHARS）"
    else
        record FAIL "/v1/characters" "HTTP $CODE count=${N_CH:-?} $(printf '%s' "$BODY" | head -c 100)"
    fi
else
    record SKIP "/v1/characters（没拿到 token）" "-"
fi

# ---------------------------------------------------------------- 7. 老形状 /chat
if [ "$DO_GENERATE" = "1" ]; then
    req POST /chat '{"character_id":"perlica","message":"用一句话确认你在。","history":[]}'
    REPLY="$(json_get 'd.get("reply","")' || true)"
    if [ "$CODE" = "200" ] && [ -n "$REPLY" ]; then
        record PASS "/chat 真出话（老形状）" "200 reply=$(printf '%s' "$REPLY" | head -c 40)…"
    else
        record FAIL "/chat 真出话" "HTTP $CODE $(printf '%s' "$BODY" | head -c 160)"
    fi
else
    # 不调上游也要证明"路由到了应用层"：空 message 应当回 400 + 统一错误体（不是 nginx 404）
    req POST /chat '{"character_id":"perlica","message":"","history":[]}'
    if [ "$CODE" = "400" ] && printf '%s' "$BODY" | grep -q '"error"'; then
        record PASS "/chat 路由通（空消息回 400 错误体）" "400 code=$(json_get 'd["error"]["code"]' || true)"
    elif [ "$CODE" = "404" ]; then
        record FAIL "/chat 没到应用层（404）" "nginx/vhost 没配 = /chat"
    else
        record FAIL "/chat 形状不对" "HTTP $CODE $(printf '%s' "$BODY" | head -c 120)"
    fi
fi

# ---------------------------------------------------------------- 8. 群聊三连（建群 → 查列表 → 关群）
req POST /group/session '{"members":["佩丽卡","陈千语"],"my_role":"admin","speak_mode":"smart","topic":"冒烟"}'
SID="$(json_get 'd.get("session_id") or d.get("sessionId") or d.get("id") or ""' || true)"
if [ "$CODE" = "200" ] && [ -n "$SID" ]; then
    record PASS "/group/session 建群" "200 session=${SID:0:10}…"
    req GET /group/sessions
    if [ "$CODE" = "200" ]; then record PASS "/group/sessions 列表" "200"; else record FAIL "/group/sessions" "HTTP $CODE"; fi
    req POST /group/session/close "{\"session_id\":\"$SID\"}"
    if [ "$CODE" = "200" ]; then record PASS "/group/session/close 关群" "200"; else record FAIL "/group/session/close" "HTTP $CODE $(printf '%s' "$BODY" | head -c 100)"; fi
else
    record FAIL "/group/session 建群" "HTTP $CODE $(printf '%s' "$BODY" | head -c 160)"
fi

# ---------------------------------------------------------------- 9. 云同步往返：/upload → /fetch
PROBE="{\"probe\":\"bakerai-smoke\",\"ts\":$(date +%s),\"note\":\"冒烟用，可删\"}"
req POST /upload "$PROBE"
UCODE="$(json_get 'd.get("code","")' || true)"
if [ "$CODE" = "200" ] && [ -n "$UCODE" ]; then
    record PASS "/upload 老形状收包 → 取件码" "200 code=$UCODE"
    req POST /fetch "{\"code\":\"$UCODE\"}"
    if [ "$CODE" = "200" ] && printf '%s' "$BODY" | grep -q 'bakerai-smoke'; then
        record PASS "/fetch 取回同一份字节" "200 $(printf '%s' "$BODY" | wc -c) bytes"
    else
        record FAIL "/fetch 取不回" "HTTP $CODE $(printf '%s' "$BODY" | head -c 120)"
    fi
else
    record FAIL "/upload 老形状" "HTTP $CODE $(printf '%s' "$BODY" | head -c 160)"
fi

# ---------------------------------------------------------------- 10. 网页同源（可选）
if [ -n "$WEB_URL" ]; then
    wcode="$(curl -sS -m 20 -o "$HTTP_BODY_FILE" -w '%{http_code}' "$WEB_URL/" 2>/dev/null || echo 000)"
    if [ "$wcode" = "200" ] && grep -q 'assets/' "$HTTP_BODY_FILE"; then
        record PASS "网页首页（静态）" "200 $(grep -o 'assets/[^"]*\.js' "$HTTP_BODY_FILE" | head -1)"
    else
        record FAIL "网页首页" "HTTP $wcode"
    fi
    wcode="$(curl -sS -m 20 -o "$HTTP_BODY_FILE" -w '%{http_code}' "$WEB_URL/v1/meta" 2>/dev/null || echo 000)"
    if [ "$wcode" = "200" ] && grep -q '"api":"v1"' "$HTTP_BODY_FILE"; then
        record PASS "网页同源 /v1/meta" "200（说明同源反代生效）"
    else
        record FAIL "网页同源 /v1/meta" "HTTP $wcode $(head -c 100 "$HTTP_BODY_FILE")"
    fi
else
    record SKIP "网页同源检查（未给 --web）" "-"
fi

# ---------------------------------------------------------------- 汇总
echo "--------------------------------------------------------------"
printf ' 合计：%sPASS %d%s  %sFAIL %d%s  %sSKIP %d%s\n' \
    "$C_GRN" "$PASS" "$C_RST" "$C_RED" "$FAIL" "$C_RST" "$C_YEL" "$SKIP" "$C_RST"
if [ "$FAIL" -gt 0 ]; then
    echo
    echo "  失败项："
    for r in "${ROWS[@]}"; do
        case "$r" in FAIL\|*) echo "    - ${r#FAIL|}";; esac
    done
    exit 1
fi
echo "  全部通过。"
exit 0
