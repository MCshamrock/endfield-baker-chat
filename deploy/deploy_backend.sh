#!/usr/bin/env bash
# deploy_backend.sh —— 把新后端代码送上服务器、装依赖、跑迁移、灌种子、起 systemd。
#
# 设计原则：**每一步都可重复跑**（rsync 覆盖、迁移按版本记、导入按唯一键覆盖），
# 任何一步失败都停下来并打印"下一步该看什么"，不会带着半套状态继续往下走。
#
# 谁跑它：跑服务的普通用户（**不要 root**）。理由：代码/venv/日志都在部署根下，
# 用 root 跑会造出一堆 root 属主的文件，之后 systemd 用户单元读写会踩权限。
# 只有两处可选 root：写 /etc/logrotate.d（sudo -n 拿不到就只警告）。
#
# 用法（先在本机把代码传到服务器暂存区，再在服务器上跑本脚本）：
#   1) 传代码（Windows 侧）：
#        pwsh -File bakeraiSSH/scripts/remote_put.ps1 -Local <tar> -Remote /tmp/bakerai-stage.tar.gz
#      或直接 rsync（服务器侧脚本会做 rsync，这里只要一个"源码目录"）
#   2) 服务器侧：
#        bash deploy_backend.sh --src /tmp/bakerai-stage/web/back
#
# 参数：
#   --src DIR            源码目录（必须含 app/main.py + requirements.lock + schema/）
#   --app DIR            部署根（默认 ~/baker-v4）
#   --db-url URL         覆盖数据库连接串（默认用 <app>/.db_password 拼）
#   --keys FILE          api_pool.json 路径（默认 /run/media/<user>/游戏/baker/backend/api_pool.json）
#   --worldbook-sql FILE 世界书 SQL 种子（默认 <本脚本>/seed/worldbook_endfield.sql）
#   --characters-v2 DIR  v2 长卡目录（默认 /run/media/<user>/游戏/baker/backend/characters_v2）
#   --expected-keys N    期望 key 总数（默认 47）
#   --expected-worldbook N 期望世界书条数（默认 147）
#   --skip-deps          不装依赖（只换代码）
#   --skip-seed          不灌种子（只迁移 + 起服务）
#   --no-restart         不重启服务（只准备文件，用于预演）
#   --dry-run            只打印
#
# 退出码：0 成功；1 任意一步失败（失败点会写明）。

SCRIPT_NAME=deploy_backend
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

SRC=""
APP_DIR="${APP_DIR:-$HOME/baker-v4}"
DB_URL=""
KEYS_FILE="${KEYS_FILE:-/run/media/$USER/游戏/baker/backend/api_pool.json}"
WORLDBOOK_SQL="$HERE/seed/worldbook_endfield.sql"
CHARS_V2="${CHARS_V2:-/run/media/$USER/游戏/baker/backend/characters_v2}"
EXPECT_KEYS=47
EXPECT_WORLDBOOK=147
SKIP_DEPS=0
SKIP_SEED=0
DO_RESTART=1
START_ONLY=0
STAGE_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --src) SRC="${2:?}"; shift 2 ;;
        --app) APP_DIR="${2:?}"; shift 2 ;;
        --db-url) DB_URL="${2:?}"; shift 2 ;;
        --keys) KEYS_FILE="${2:?}"; shift 2 ;;
        --worldbook-sql) WORLDBOOK_SQL="${2:?}"; shift 2 ;;
        --characters-v2) CHARS_V2="${2:?}"; shift 2 ;;
        --expected-keys) EXPECT_KEYS="${2:?}"; shift 2 ;;
        --expected-worldbook) EXPECT_WORLDBOOK="${2:?}"; shift 2 ;;
        --skip-deps) SKIP_DEPS=1; shift ;;
        --skip-seed) SKIP_SEED=1; shift ;;
        --no-restart) DO_RESTART=0; shift ;;
        --stage) DO_RESTART=0; STAGE_ONLY=1; shift ;;
        --start) START_ONLY=1; DO_RESTART=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

BACK_DIR="$APP_DIR/back"
VENV="$APP_DIR/venv"
PY="$VENV/bin/python"
LOGS="$APP_DIR/logs"
UNIT_DIR="$HOME/.config/systemd/user"
PASS_FILE="$APP_DIR/.db_password"
ENV_FILE="$BACK_DIR/.env"
UNIT_NAME=baker-api.service
OLD_UNIT=baker-v4.service

export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

# ------------------------------------------------------------------ 工具
# mysql 客户端：密码不放在命令行（ps 里看得见），用临时 defaults 文件
MYSQL_CNF=""
mysql_init() {
    local pass="$1"
    MYSQL_CNF="$(mktemp /tmp/bakerai-my.XXXXXX.cnf)"
    chmod 600 "$MYSQL_CNF"
    cat >"$MYSQL_CNF" <<EOF
[client]
host=127.0.0.1
port=3306
user=baker
password=$pass
default-character-set=utf8mb4
EOF
    trap 'rm -f "$MYSQL_CNF" "$HTTP_BODY_FILE"' EXIT
}
q() { mysql --defaults-extra-file="$MYSQL_CNF" -N -B -e "$1" "$2" 2>/dev/null; }

# 从 BAKER_DATABASE_URL 里抠库名（不打印密码）
db_name_of_url() {
    python3 - "$1" <<'PY'
import sys,urllib.parse as up
u=up.urlparse(sys.argv[1].replace("mysql+pymysql","mysql"))
print((u.path or "/").lstrip("/") or "baker_back")
PY
}

step "0. 预检（缺什么现在就说，别等第 6 步再炸）"
if [ "$START_ONLY" = "1" ]; then
    ok "--start：只做 systemd 启动 + 健康验证（跳过代码/依赖/迁移/种子）"
else
    [ -n "$SRC" ] || die "必须给 --src <源码目录>（-h 看用法）"
    [ -f "$SRC/app/main.py" ] || die "$SRC/app/main.py 不存在，--src 指错了？"
    [ -f "$SRC/requirements.lock" ] || die "$SRC/requirements.lock 不存在（依赖版本必须按锁文件装）"
    [ -d "$SRC/schema" ] || die "$SRC/schema 不存在"
fi
need_cmd rsync python3 curl ss systemctl

PY314=/usr/bin/python3.14
[ -x "$PY314" ] || die "没有 /usr/bin/python3.14（服务器当前应该是 3.14.4）"
ok "python3.14 = $("$PY314" -V 2>&1)"

[ -s "$PASS_FILE" ] || die "缺 $PASS_FILE（数据库密码文件，600）。先确认 MySQL 已装并建号（README T-24h 第 2 步）"
DB_PASS="$(cat "$PASS_FILE")"
mysql_init "$DB_PASS"
if [ -z "$DB_URL" ]; then
    DB_URL="mysql+pymysql://baker:${DB_PASS}@127.0.0.1:3306/baker_back?charset=utf8mb4"
fi
DB_NAME="$(db_name_of_url "$DB_URL")"
q "select 1" "$DB_NAME" >/dev/null || die "数据库连不上：$DB_NAME（检查 $PASS_FILE 与 mysql 服务）"
ok "数据库连通：$DB_NAME，当前迁移版本 $(q 'select max(version) from schema_versions' "$DB_NAME" || echo '无')"

if port_listening 5660; then
    OWNER_UNIT="$(systemctl --user list-units --no-legend 2>/dev/null | awk '$4=="running"{print $1}' | grep -E 'baker-v4|baker-api' | tr '\n' ' ' || true)"
    log "5660 已被占用（pid: $(port_pids 5660)）；相关单元：${OWNER_UNIT:-未知}"
else
    ok "5660 空闲"
fi

# --start 时整段跳过；--stage/--no-restart 时照做但不起服务
if [ "$START_ONLY" = "0" ]; then

step "1. 同步代码 → $BACK_DIR"
if [ "$DRY_RUN" = "0" ]; then mkdir -p "$APP_DIR" "$LOGS"; fi
# 三类排除：
#   .env       —— 服务器上的配置（含密码/密钥），绝不能被仓库里的开发配置覆盖
#   .venv*     —— 本机/另一套平台的虚拟环境
#   _runtime   —— 上传与临时文件（这是数据，不是代码）
RSYNC_OPTS=(-a --delete --human-readable
    --exclude='.env' --exclude='.env.*' --exclude='.venv' --exclude='.venv-*'
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.mypy_cache'
    --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='_runtime'
    --exclude='*.egg-info' --exclude='logs')
run rsync "${RSYNC_OPTS[@]}" "$SRC/" "$BACK_DIR/"

step "2. .env（缺了才生成；已有就一个字都不动）"
if [ -f "$ENV_FILE" ] && [ "$DRY_RUN" = "0" ]; then
    ok ".env 已存在（$(stat -c '%a %U:%G' "$ENV_FILE")），保留不动"
    grep -q '^BAKER_SECRET=' "$ENV_FILE" || warn ".env 里没有 BAKER_SECRET —— 导 key 会加密失败，请补一行"
else
    log "生成 $ENV_FILE（600）"
    if [ "$DRY_RUN" = "0" ]; then
        SECRET="$(openssl rand -hex 24)"
        (umask 077; cat >"$ENV_FILE" <<EOF
# 生产配置（由 deploy_backend.sh 生成；含密钥，600，绝不进 git）
BAKER_DATABASE_URL=$DB_URL
BAKER_HOST=127.0.0.1
BAKER_PORT=5660
BAKER_LOG_LEVEL=INFO

BAKER_API_VERSION=v1
BAKER_SCHEMA_VERSION=1
BAKER_APP_MIN=3.0.0
BAKER_PRODUCT=lite
BAKER_GEN_MODE=inline

# 上游：真出话必须有 key；这里留空，key 从 api_keys 表里取（import_keys.py 导入）
BAKER_UPSTREAM_BASE_URL=https://apihub.agnes-ai.com/v1
BAKER_UPSTREAM_MODEL=agnes-2.5-flash
BAKER_UPSTREAM_API_KEY=
BAKER_UPSTREAM_TIMEOUT=60
BAKER_UPSTREAM_MAX_TOKENS=2048
BAKER_UPSTREAM_TEMPERATURE=0.8

# key 加密密钥：换了它，库里已存的 key 全部解不开
BAKER_SECRET=$SECRET

# 上传/临时文件必须留在项目内（默认会算到 ~/_runtime，那是项目外）
BAKER_UPLOAD_DIR=$APP_DIR/_runtime/uploads
BAKER_TMP_DIR=$APP_DIR/_runtime/tmp

BAKER_WORLDBOOK_ENABLED=true
BAKER_WORLDBOOK_SLUG=endfield
BAKER_USER_RPM=12
BAKER_KEY_WAIT_SECONDS=20
BAKER_UPLOAD_TTL_HOURS=24
EOF
        )
        chmod 600 "$ENV_FILE"
        ok ".env 写好（BAKER_SECRET 随机生成，未打印）"
    fi
fi
if [ "$DRY_RUN" = "0" ]; then
    mkdir -p "$APP_DIR/_runtime/uploads" "$APP_DIR/_runtime/tmp"
    ok "上传目录就绪：$APP_DIR/_runtime/{uploads,tmp}"
fi

step "3. venv 与依赖（按 requirements.lock 装，不按 pyproject 范围解析）"
if [ ! -x "$PY" ]; then
    log "创建 venv：$PY314 -m venv $VENV"
    run "$PY314" -m venv "$VENV"
else
    ok "venv 已存在：$("$PY" -V 2>&1)（$VENV）"
fi
if [ "$SKIP_DEPS" = "1" ]; then
    warn "--skip-deps：跳过依赖安装（新代码如果用了没装的包，起服务会 ImportError）"
else
    # 已经满足的包 pip 会跳过；只有新加的（cryptography / python-multipart）会真下载。
    # 本机实测 pip 源可达但慢（全量 889s）；2 个新包预计 20–90s。
    log "安装运行时依赖（从 requirements.lock 过滤，跳过 mypy/ruff/pytest 等开发包）"
    # 为什么不全量装 lock：里面还有 pytest/mypy/ruff 等 20+ MB 开发包，
    # 服务器到 PyPI 只有 ~13.5 kB/s（实测 cryptography 4.8 MB 用了 6 分钟），
    # 全量装会把 2 点的窗口吃光。生产运行时只需要下面这些。
    WHEELS=/tmp/bakerai-wheels
    DEV_PKGS='^(mypy|mypy_extensions|pathspec|librt|ruff|colorama|pytest|iniconfig|pluggy|Pygments)=='
    mapfile -t RUNTIME_PINS < <(grep -vE '^[[:space:]]*#' "$BACK_DIR/requirements.lock"         | grep -E '^[A-Za-z0-9_.-]+=='         | grep -vE "$DEV_PKGS")
    log "运行时包 ${#RUNTIME_PINS[@]} 个"
    PIP_ARGS=(--disable-pip-version-check --no-input -q)
    if [ -d "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
        log "发现本地 wheel：$WHEELS（$(ls -1 "$WHEELS"/*.whl | wc -l) 个），优先离线安装"
        PIP_ARGS+=(--find-links "$WHEELS")
    fi
    run "$PY" -m pip install "${PIP_ARGS[@]}" "${RUNTIME_PINS[@]}"
    if [ "$DRY_RUN" = "0" ]; then
        "$PY" - <<'PY' || die "关键依赖导入失败（cryptography 用于 key 加密、python-multipart 用于上传）"
import importlib
for m in ("fastapi","uvicorn","sqlalchemy","pymysql","pydantic_settings","cryptography","multipart"):
    importlib.import_module(m)
print("  ✓ 关键依赖可导入：fastapi uvicorn sqlalchemy pymysql pydantic_settings cryptography multipart")
PY
    fi
fi

step "4. 迁移（schema/001..019；已跑过的不会重跑）"
run_masked "env BAKER_DATABASE_URL=$(mask_url "$DB_URL") $PY $BACK_DIR/scripts/migrate.py"         env BAKER_DATABASE_URL="$DB_URL" "$PY" "$BACK_DIR/scripts/migrate.py"
if [ "$DRY_RUN" = "0" ]; then
    LAST_MIG="$(q 'select max(version) from schema_versions' "$DB_NAME")"
    TABLES="$(q 'select count(*) from information_schema.tables where table_schema=DATABASE()' "$DB_NAME")"
    ok "最后迁移 = $LAST_MIG，表数 = $TABLES"
    case "$LAST_MIG" in
        019*) ok "迁移到 019_prompt_variant（预期）" ;;
        *) die "迁移没跑到 019（当前 $LAST_MIG）。先看 migrate 的输出，常见原因：SQL 语法/权限/库名不对" ;;
    esac
fi

if [ "$SKIP_SEED" = "1" ]; then
    warn "--skip-seed：跳过种子（key 池/世界书/v2 长卡都不会灌）"
else
    step "5. 导 key 池（$KEYS_FILE → api_keys 表，AES-256-GCM 加密）"
    [ -f "$KEYS_FILE" ] || die "找不到 $KEYS_FILE"
    # 这条命令里同时有数据库密码和 BAKER_SECRET —— 打印时必须打码
    run_masked "env BAKER_DATABASE_URL=$(mask_url "$DB_URL") BAKER_SECRET=<从 .env 读取> $PY scripts/import_keys.py --file $KEYS_FILE" \
        env BAKER_DATABASE_URL="$DB_URL" \
            BAKER_SECRET="$(sed -n 's/^BAKER_SECRET=//p' "$ENV_FILE" | head -1)" \
            "$PY" "$BACK_DIR/scripts/import_keys.py" --file "$KEYS_FILE"
    if [ "$DRY_RUN" = "0" ]; then
        read -r K_TOTAL K_ENABLED < <(q "select count(*), sum(enabled=1) from api_keys" "$DB_NAME" | awk '{print $1, $2}')
        ok "api_keys: 总数 $K_TOTAL / 启用 ${K_ENABLED:-0}（期望总数 $EXPECT_KEYS）"
        [ "${K_TOTAL:-0}" -eq "$EXPECT_KEYS" ] || die "key 数量不对（$K_TOTAL ≠ $EXPECT_KEYS），检查 api_pool.json 是不是最新那份"
    fi

    step "6. 世界书（147 条 endfield 设定）—— 不跑这步，检索等于没有"
    # 首选「source.md + import_worldbook.py」：这是权威路径（lead 2026-11-18 在全新库上实测 147 条）。
    # 没有 md 时退回 SQL 种子（同一批数据导出的副本，REPLACE INTO 幂等）。
    WB_MD="$HERE/seed/终末地世界书_通用版_v1.0.md"
    if [ -f "$WB_MD" ]; then
        log "用 source.md 导入：$WB_MD"
        run_masked "env BAKER_DATABASE_URL=$(mask_url "$DB_URL") $PY scripts/import_worldbook.py --file <seed.md> --world endfield"             env BAKER_DATABASE_URL="$DB_URL" "$PY" "$BACK_DIR/scripts/import_worldbook.py"             --file "$WB_MD" --world endfield
    elif [ -f "$WORLDBOOK_SQL" ]; then
        log "没有 source.md，退回 SQL 种子：$WORLDBOOK_SQL"
        if [ "$DRY_RUN" = "0" ]; then
            mysql --defaults-extra-file="$MYSQL_CNF" "$DB_NAME" <"$WORLDBOOK_SQL"
        fi
    else
        die "世界书源文件与 SQL 种子都不在（$WB_MD / $WORLDBOOK_SQL）—— 线上会没有世界书"
    fi
    if [ "$DRY_RUN" = "0" ]; then
        WB="$(q "select count(*) from worldbook where world_slug='endfield'" "$DB_NAME")"
        ok "worldbook: $WB 条（期望 $EXPECT_WORLDBOOK）"
        [ "${WB:-0}" -eq "$EXPECT_WORLDBOOK" ] || die "世界书条数不对（$WB ≠ $EXPECT_WORLDBOOK）"
    fi

    step "7. v2 长卡（characters_v2/*.json → character_prompts variant=v2）"
    if [ -d "$CHARS_V2" ]; then
        run_masked "env BAKER_DATABASE_URL=$(mask_url "$DB_URL") $PY scripts/import_prompts_v2.py --source $CHARS_V2"             env BAKER_DATABASE_URL="$DB_URL" "$PY" "$BACK_DIR/scripts/import_prompts_v2.py" --source "$CHARS_V2"
        if [ "$DRY_RUN" = "0" ]; then
            V2="$(q "select count(*) from character_prompts where variant='v2' and active=1" "$DB_NAME")"
            CH="$(q 'select count(*) from characters' "$DB_NAME")"
            ok "characters=$CH，v2 提示词=$V2（期望 ≥36）"
            [ "${V2:-0}" -ge 36 ] || warn "v2 提示词少于 36 条，检查 $CHARS_V2 里是不是 38 个文件（含 index.json）"
        fi
    else
        warn "找不到 $CHARS_V2（v2 长卡目录），跳过"
    fi
fi

    if [ "$STAGE_ONLY" = "1" ]; then
        ok "--stage 完成：代码/依赖/迁移/种子全部就位"
        log "服务**没有**启动、unit **没有**安装、5660 上的影子 baker-v4 **没有**被动过"
        log "窗口里只需：deploy_backend.sh --start   （秒级）"
        exit 0
    fi

fi   # ← --start 时跳过的边界（1-7 铺路，8-9 点火）

step "8. systemd 用户单元（baker-api.service）"
mkdir -p "$UNIT_DIR"
if [ "$DRY_RUN" = "0" ]; then
    sed "s#%h/baker-v4#$APP_DIR#g" "$HERE/systemd/baker-api.service" >"$UNIT_DIR/$UNIT_NAME"
    ok "写入 $UNIT_DIR/$UNIT_NAME"
    # 老的影子单元占着同一个 5660：必须先停，否则新单元起不来（Address already in use）
    if systemctl --user is-enabled "$OLD_UNIT" >/dev/null 2>&1 || systemctl --user is-active "$OLD_UNIT" >/dev/null 2>&1; then
        log "停用旧影子单元 $OLD_UNIT（它占着 5660）"
        systemctl --user disable --now "$OLD_UNIT" 2>/dev/null || true
        # 顺序写死：停影子 → 等端口真释放 → 再起新服务。
        # 不等这一步的话，systemd 起 baker-api 会 Address already in use，Restart=always 会一直重试。
        for _ in $(seq 1 10); do port_listening 5660 || break; sleep 1; done
        if port_listening 5660; then
            die "停掉 $OLD_UNIT 后 5660 仍被占用（pid: $(port_pids 5660)）。手工确认后再跑 --start"
        fi
        ok "5660 已释放（影子已停并 disable，重启后不会自启）"
    fi
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT_NAME" >/dev/null 2>&1 || warn "enable 失败（重启后不会自启）"
    ok "baker-api 已 enable"
    # logrotate（可选 root）：不写的话日志会一直长
    if sudo_ok; then
        as_root tee /etc/logrotate.d/baker-api >/dev/null <<EOF
$LOGS/*.log {
    su y y
    daily
    rotate 7
    maxsize 50M
    compress
    missingok
    notifempty
    copytruncate
}
EOF
        ok "logrotate 已配置 /etc/logrotate.d/baker-api"
    else
        warn "没有免密 sudo，跳过 logrotate（日志无限增长；可事后手工加）"
    fi
else
    log "  \$ sed ... > $UNIT_DIR/$UNIT_NAME; systemctl --user daemon-reload; enable --now baker-api"
fi

step "9. 起服务 + 验证（端口在听 / HTTP 200 / readyz 真通 / key 数对）"
if [ "$DO_RESTART" = "0" ]; then
    warn "--no-restart：不重启，服务仍是旧代码"
else
    run systemctl --user restart "$UNIT_NAME"
    if [ "$DRY_RUN" = "0" ]; then
        wait_http "http://127.0.0.1:5660/health" 200 30 || { journalctl --user -u "$UNIT_NAME" -n 40 --no-pager; die "/health 30 秒内没起来"; }
        ok "/health 200"
        wait_http "http://127.0.0.1:5660/readyz" 200 30 || { journalctl --user -u "$UNIT_NAME" -n 40 --no-pager; die "/readyz 不通（库/迁移有问题）"; }
        ok "/readyz 200：$(http_body)"
        code="$(http_code "http://127.0.0.1:5660/instances")"
        [ "$code" = "200" ] || die "/instances 返回 $code"
        K="$(json_get 'd["keys"]["total"]' || true)"
        E="$(json_get 'd["keys"]["enabled"]' || true)"
        ok "/instances keys.total=$K enabled=$E"
        [ "${K:-0}" -eq "$EXPECT_KEYS" ] || warn "keys.total=$K ≠ $EXPECT_KEYS"
        code="$(http_code "http://127.0.0.1:5660/v1/meta")"
        [ "$code" = "200" ] || die "/v1/meta 返回 $code"
        ok "/v1/meta 200：caps=$(json_get '",".join(d["caps"])')"
        echo
        echo "  进程：pid $(port_pids 5660)  RSS $(ps -o rss= -p "$(port_pids 5660 | awk '{print $1}')" 2>/dev/null | awk '{printf "%.0f MB", $1/1024}')"
        echo "  日志：$LOGS/baker-api.log"
    fi
fi

printf '\n%s后端部署完成%s\n' "$C_GRN" "$C_RST"
echo " 下一步：switch_nginx.sh --web both   （切 nginx 反代）"
echo " 回滚：  rollback.sh                  （nginx 回旧 upstream，旧 gateway 一直没停）"
