#!/usr/bin/env bash
# lib/common.sh —— 所有部署脚本共用的几件事：日志、root 判定、HTTP/JSON 小工具。
#
# 为什么单独一份：这 6 个脚本要在同一台机器上反复跑，最容易出事的是
# 「这个脚本用了 sudo、那个没有」和「报错都长得一样，看不出是谁挂的」。
# 所以统一在这里定死：谁能提权、日志长什么样、失败时退出码是多少。
#
# 用法（在每个脚本开头）：
#   HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=lib/common.sh
#   . "$HERE/lib/common.sh"

set -euo pipefail

# ---- 颜色（没有 tty 时自动关掉，日志重定向到文件不会带一堆转义符） ----
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_CYN=$'\033[36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
    C_RED=''; C_GRN=''; C_YEL=''; C_CYN=''; C_DIM=''; C_RST=''
fi

SCRIPT_NAME="${SCRIPT_NAME:-$(basename "${BASH_SOURCE[1]:-$0}" .sh)}"

log()  { printf '%s[%s]%s %s\n' "$C_CYN" "$SCRIPT_NAME" "$C_RST" "$*"; }
step() { printf '\n%s==== %s ====%s\n' "$C_CYN" "$*" "$C_RST"; }
ok()   { printf '%s  ✓ %s%s\n' "$C_GRN" "$*" "$C_RST"; }
warn() { printf '%s  ! %s%s\n' "$C_YEL" "$*" "$C_RST" >&2; }
die()  { printf '%s  ✗ %s%s\n' "$C_RED" "$*" "$C_RST" >&2; exit "${2:-1}"; }

# 干跑开关：只打印将要做什么，不落盘、不 reload、不重启。
# 约定所有脚本都认 --dry-run，且 **任何写操作都必须先过这里**。
DRY_RUN="${DRY_RUN:-0}"
run() {
    # 把「要执行的命令」原样打出来再执行；dry-run 时只打印。
    printf '%s  $ %s%s\n' "$C_DIM" "$*" "$C_RST"
    if [ "$DRY_RUN" = "1" ]; then return 0; fi
    "$@"
}

# 带密码/密钥的命令用它：屏幕（和日志）里只出现打码后的样子，真值只进参数。
# 用法：run_masked "<给人看的命令>" <真实命令...>
mask_url() {
    # 纯 bash 写法，不用 sed 正则（少一层转义，读起来也不会眼花）：
    #   mysql+pymysql://baker:SECRET@127.0.0.1:3306/db
    #   pre  = mysql+pymysql://baker:SECRET    （@ 之前）
    #   rest = 127.0.0.1:3306/db              （@ 之后）
    local url="$1" pre rest
    pre="${url%%@*}"
    rest="${url#*@}"
    if [ "$pre" = "$url" ]; then
        printf '%s' "$url"
    else
        printf '%s:***@%s' "${pre%:*}" "$rest"
    fi
}

run_masked() {
    local display="$1"; shift
    printf '%s  $ %s%s' "$C_DIM" "$display" "$C_RST"
    echo
    if [ "$DRY_RUN" = "1" ]; then return 0; fi
    "$@"
}

# ---- root ----
is_root() { [ "$(id -u)" -eq 0 ]; }

# 需要 root 的脚本：不是 root 就直接说清楚怎么跑，别在中间某步莫名其妙失败。
# 例外：--dry-run 只打印不动手，允许非 root 跑（本地演练就靠这条）。
require_root() {
    is_root && return 0
    if [ "$DRY_RUN" = "1" ]; then
        warn "非 root 且 --dry-run：只打印，不会真的写文件/重载服务"
        return 0
    fi
    die "这个脚本要 root。用远程通道跑：
    pwsh -File bakeraiSSH/scripts/remote.ps1 -SshHost baker -ScriptFile <本脚本> -Sudo"
}

# 只需要个别 root 操作的脚本用它：sudo -n 拿不到就不硬撑（避免脚本卡在密码提示上）。
as_root() {
    if is_root; then "$@"; else sudo -n "$@"; fi
}

# 探测 sudo 是否免密可用（给"可选 root 步骤"用，比如装 logrotate 配置）
sudo_ok() { is_root || sudo -n true 2>/dev/null; }

# ---- 常用小工具 ----
need_cmd() {
    local c
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "缺命令：$c"
    done
}

stamp() { date +%Y%m%d_%H%M%S; }

sha_of() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }

# 端口上有没有人听（ss 一定有，服务器是 Ubuntu 26.04）
port_listening() { ss -lntH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }

# 谁占着端口（输出 pid，可能空）
port_pids() {
    ss -lntpH 2>/dev/null | awk -v p=":$1\$" '$4 ~ p' \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u | tr '\n' ' '
}

# http_code URL [...] → 打印状态码，正文写到 $HTTP_BODY_FILE
HTTP_BODY_FILE="$(mktemp -t bakerai-http.XXXXXX)"
trap 'rm -f "$HTTP_BODY_FILE"' EXIT
http_code() {
    curl -sS -m "${HTTP_TIMEOUT:-20}" -o "$HTTP_BODY_FILE" -w '%{http_code}' "$@" 2>/dev/null || echo "000"
}
http_body() { cat "$HTTP_BODY_FILE" 2>/dev/null; }

# 从文件读 JSON 并求值（不依赖 jq）。表达式里的 d = 解析出来的对象。
# 例：json_file_get /tmp/x.json 'd["keys"]["total"]'
json_file_get() {
    python3 - "$1" "$2" <<'PY' 2>/dev/null || echo ""
import json,sys
path,expr=sys.argv[1],sys.argv[2]
try:
    d=json.load(open(path,encoding="utf-8"))
except Exception:
    print(""); raise SystemExit(0)
try:
    print(eval(expr,{"__builtins__":{}},{"d":d,"len":len,"str":str,"int":int,"any":any,"all":all,"sorted":sorted}))
except Exception:
    print("")
PY
}

# 从最近一次 http_code 的正文里取值
json_get() { json_file_get "$HTTP_BODY_FILE" "${1:-d}"; }

# 等某个 URL 返回 200，最多等 N 秒（部署后起服务用）
wait_http() {
    local url="$1" want="${2:-200}" secs="${3:-30}" i code
    for ((i = 0; i < secs; i++)); do
        code="$(http_code "$url")"
        [ "$code" = "$want" ] && return 0
        sleep 1
    done
    return 1
}

# 打印文件清单 + sha256（备份/交付都要这一步，不做就等于没证据）
print_sha256() {
    local f
    for f in "$@"; do
        [ -f "$f" ] || continue
        printf '  %s  %8s  %s\n' "$(sha_of "$f")" "$(du -h "$f" | cut -f1)" "$f"
    done
}
