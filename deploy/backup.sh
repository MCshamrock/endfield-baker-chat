#!/usr/bin/env bash
# backup.sh —— 上线前的"后悔药"：把这次会动到的东西全部打包到 /opt/bakerai-backup/<时间戳>/
#
# 覆盖范围（每一项都对应回滚时真的会用到的动作）：
#   1. 老后端代码        /run/media/<user>/游戏/baker/backend   → 回滚后老服务照原样跑
#   2. 新后端影子代码    ~/baker-v4/back                       → 部署失败时退回上一版新代码
#   3. nginx 全配置      /etc/nginx                             → rollback.sh 直接还原
#   4. api_pool.json     /run/media/<user>/游戏/baker/backend/  → key 池的唯一副本（600）
#   5. 群聊 SQLite       run/group_sessions.db(+wal/shm)         → 老群聊会话（WAL 里有未落盘数据）
#   6. 两块 web dist     /var/www/peilika-chat, /var/www/peilika-v2 → 覆盖前的老前端
#   7. MySQL 全库        baker_back / baker_back_test            → 迁移改坏了能整个倒回去
#   8. 配置快照          systemd 用户单元 / logrotate / cloudflared yml
#
# 铁律：**先备份，再覆盖**。本脚本只读源、只写目标目录，不改任何生产文件。
#
# 用法（必须 root：/etc/nginx 有 600 的文件、/opt 要 root 才能建）：
#   pwsh -File bakeraiSSH/scripts/remote.ps1 -SshHost baker -ScriptFile <本脚本> -Sudo
# 常用参数：
#   --dest DIR     备份根目录（默认 /opt/bakerai-backup）
#   --tag NAME     给这次备份起个名（默认 deploy），目录名 = <时间戳>_<tag>
#   --no-db        跳过 mysqldump（只想备文件时用，快 5 秒）
#   --dry-run      只打印要做什么

SCRIPT_NAME=backup
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

DEST_ROOT=/opt/bakerai-backup
TAG=deploy
DO_DB=1

OLD_BACKEND="${OLD_BACKEND:-/run/media/$USER/游戏/baker/backend}"
NEW_BACKEND="${NEW_BACKEND:-$HOME/baker-v4/back}"
WEB_PROD="/var/www/peilika-chat"     # chat.peilika.beer（正式）
WEB_STAGE="/var/www/peilika-v2"      # test2.peilika.beer（预演）

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST_ROOT="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --no-db) DO_DB=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1（--help 看用法）" ;;
    esac
done

require_root
need_cmd tar gzip sha256sum mysql python3

TS="$(stamp)"
DIR="$DEST_ROOT/${TS}_${TAG}"
LOGF="$DIR/BACKUP.log"
INFO="$DIR/INFO.txt"
MANIFEST="$DIR/MANIFEST.sha256"
SKIPPED="$DIR/SKIPPED.txt"

# 失败也要留证据：记下是哪一步挂的，别让人猜。
CURRENT_STEP="(未开始)"
on_err() {
    local rc=$?
    printf 'FAILED step=%s rc=%s at %s\n' "$CURRENT_STEP" "$rc" "$(date -Is)" >>"$LOGF" 2>/dev/null || true
    printf '%s备份中断在：%s（rc=%s）%s\n' "$C_RED" "$CURRENT_STEP" "$rc" "$C_RST" >&2
    printf '已写入的部分在：%s\n' "$DIR" >&2
    exit "$rc"
}
trap on_err ERR

step "0. 准备目录"
log "备份目录：$DIR"
if [ "$DRY_RUN" = "0" ]; then
    mkdir -p "$DIR"
    chmod 750 "$DEST_ROOT" "$DIR"
    : >"$LOGF"
    exec > >(tee -a "$LOGF") 2>&1
fi

step "1. 环境快照（回滚时要对得上"当时是什么版本"）"
if [ "$DRY_RUN" = "0" ]; then
{
    echo "# BakerAI 上线前备份"
    echo "time: $(date -Is)"
    echo "host: $(hostname)"
    echo "kernel: $(uname -r)"
    echo "os: $(. /etc/os-release && echo "$PRETTY_NAME")"
    echo "python3: $(python3 -V 2>&1)"
    echo "python3.14: $(/usr/bin/python3.14 -V 2>&1 || echo '无')"
    echo "mysql: $(mysql --version 2>&1)"
    echo "nginx: $(nginx -v 2>&1)"
    echo "node: $(node -v 2>&1 || echo '无')"
    echo "cloudflared: $(cloudflared --version 2>&1 || echo '无')"
    echo "--- 磁盘 ---"
    df -h /
    echo "--- 内存 ---"
    free -m
    echo "--- 5660 / 3306 / 80 ---"
    ss -lntH 2>/dev/null | grep -E ':(5660|3306|80)\b' || true
    echo "--- 用户单元（新后端） ---"
    XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active baker-v4 baker-api 2>&1 || true
} >"$INFO" 2>&1 || warn "环境快照有失败项（不阻塞备份）"
ok "INFO.txt $(du -h "$INFO" | cut -f1)"
else
    log "  (dry-run) 跳过 INFO.txt 写入"
fi

# 通用打包函数：路径不存在就记 SKIP，不让整个备份挂掉
pack() {
    local name="$1" src="$2"; shift 2
    CURRENT_STEP="pack:$name"
    if [ ! -e "$src" ]; then
        printf '%s\t源不存在，跳过\n' "$name" >>"$SKIPPED"
        warn "跳过 $name（$src 不存在）"
        return 0
    fi
    log "打包 $name ← $src"
    if [ "$DRY_RUN" = "1" ]; then
        printf '  $ tar -czf %s/%s.tar.gz -C %s %s\n' "$DIR" "$name" "$(dirname "$src")" "$(basename "$src")"
        return 0
    fi
    tar -czf "$DIR/$name.tar.gz" -C "$(dirname "$src")" "$@" "$(basename "$src")"
    ok "$name.tar.gz $(du -h "$DIR/$name.tar.gz" | cut -f1)（$(tar -tzf "$DIR/$name.tar.gz" | wc -l) 个条目）"
}

step "2. 老后端代码（正式 perlica，含 .env 与 47 把 key 的池文件）"
# 排掉三类：日志（3.8G）、字节码缓存、历史 .bak（几百个，纯噪音）
pack old-backend-code "$OLD_BACKEND" \
    --exclude='logs' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.bak_*' --exclude='api_pool.json.bak*' --exclude='*.log'

step "3. 新后端影子代码（$NEW_BACKEND，含 .env）"
pack new-backend-code "$NEW_BACKEND" \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='_runtime'

step "4. nginx 全配置"
pack nginx-config /etc/nginx
# 单独留一份"当前启用的站点清单"，回滚时一眼看出该还原哪几个
CURRENT_STEP="nginx-list"
if [ "$DRY_RUN" = "0" ]; then
    ls -la /etc/nginx/sites-enabled/ >"$DIR/nginx-sites-enabled.ls" 2>&1 || true
    nginx -T >"$DIR/nginx-full.txt" 2>&1 || warn "nginx -T 失败（配置本身有问题，建议先查）"
fi

step "5. api_pool.json（47 把 key，权限 600 保持）"
CURRENT_STEP="api_pool"
if [ -f "$OLD_BACKEND/api_pool.json" ]; then
    if [ "$DRY_RUN" = "0" ]; then
        install -m 600 -o root -g root "$OLD_BACKEND/api_pool.json" "$DIR/api_pool.json"
        python3 - "$DIR/api_pool.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
apis=d.get("apis") or []
print(f"  api_pool.json: {len(apis)} 个 key，启用 {sum(1 for a in apis if a.get('enabled',True))} 个")
PY
    else
        log "  \$ install -m 600 $OLD_BACKEND/api_pool.json $DIR/api_pool.json"
    fi
else
    printf 'api_pool\t源不存在\n' >>"$SKIPPED"; warn "api_pool.json 不存在！新后端就没有 key 池了"
fi

step "6. 群聊 SQLite（在线备份 + 原始 WAL/SHM 一并留）"
CURRENT_STEP="sqlite"
DB_FILE="$OLD_BACKEND/run/group_sessions.db"
if [ -f "$DB_FILE" ]; then
    if [ "$DRY_RUN" = "0" ]; then
        # 直接 cp 一个正在写的 SQLite 可能拿到"半个事务"；用 Python 的 backup API
        # 走 SQLite 自己的在线备份协议，WAL 里没落盘的数据也会一起带过去。
        python3 - "$DB_FILE" "$DIR/group_sessions.db" <<'PY'
import sqlite3,sys
src,dst=sys.argv[1],sys.argv[2]
s=sqlite3.connect(f"file:{src}?mode=ro",uri=True)
d=sqlite3.connect(dst)
with d: s.backup(d)
d.close(); s.close()
print("  sqlite online backup ok")
PY
        # 原始文件也留一份：万一以后要查 WAL，这是当时的字面状态
        tar -czf "$DIR/group_sessions-raw.tar.gz" -C "$(dirname "$DB_FILE")" \
            "$(basename "$DB_FILE")" "$(basename "$DB_FILE")-wal" "$(basename "$DB_FILE")-shm" 2>/dev/null \
            || tar -czf "$DIR/group_sessions-raw.tar.gz" -C "$(dirname "$DB_FILE")" "$(basename "$DB_FILE")"
        ok "group_sessions.db $(du -h "$DIR/group_sessions.db" | cut -f1)"
    else
        log "  \$ python3 (sqlite3 online backup) $DB_FILE → $DIR/group_sessions.db"
    fi
else
    printf 'group_sessions\t源不存在\n' >>"$SKIPPED"; warn "群聊 SQLite 不存在（老群聊可能已经没在跑）"
fi

step "7. 两块 web dist（覆盖前的老前端）"
pack web-prod-dist "$WEB_PROD"
pack web-stage-dist "$WEB_STAGE"
# 记录当前 index.html 的哈希：回滚后用它证明"真的换回去了"
CURRENT_STEP="web-hash"
if [ "$DRY_RUN" = "0" ]; then
    {
        echo "== 部署前 web dist 指纹 =="
        sha256sum "$WEB_PROD/index.html" "$WEB_STAGE/index.html" 2>/dev/null || true
        stat -c '%y %s %n' "$WEB_PROD/index.html" "$WEB_STAGE/index.html" 2>/dev/null || true
    } >"$DIR/web-before.txt" 2>&1 || true
fi

step "8. MySQL 全库（迁移前必须有一份能整个倒回去的 dump）"
if [ "$DO_DB" = "1" ]; then
    CURRENT_STEP="mysqldump"
    for db in baker_back baker_back_test; do
        if mysql -uroot -N -e "select 1 from information_schema.schemata where schema_name='$db'" 2>/dev/null | grep -q 1; then
            if [ "$DRY_RUN" = "0" ]; then
                mysqldump -uroot --single-transaction --routines --triggers \
                    --default-character-set=utf8mb4 "$db" 2>/dev/null | gzip -9 >"$DIR/mysql-$db.sql.gz"
                ok "mysql-$db.sql.gz $(du -h "$DIR/mysql-$db.sql.gz" | cut -f1)"
            else
                log "  \$ mysqldump -uroot --single-transaction $db | gzip > $DIR/mysql-$db.sql.gz"
            fi
        else
            printf 'mysql-%s\t库不存在\n' "$db" >>"$SKIPPED"
        fi
    done
else
    warn "--no-db：跳过 MySQL 备份（回滚就只能靠"库不动"，没有倒回去的路）"
fi

step "9. 配置快照（systemd 用户单元 / logrotate / cloudflared）"
CURRENT_STEP="configs"
if [ "$DRY_RUN" = "0" ]; then
    tar -czf "$DIR/configs.tar.gz" \
        -C / \
        home/y/.config/systemd/user/baker-v4.service \
        home/y/.config/systemd/user/baker-api.service \
        home/y/.config/systemd/user/perlica-gateway.service \
        etc/logrotate.d/baker-v4 \
        etc/logrotate.d/baker-api \
        etc/cloudflared/config.yml etc/cloudflared/config-ssh.yml \
        2>/dev/null || true
    [ -f "$DIR/configs.tar.gz" ] && ok "configs.tar.gz $(du -h "$DIR/configs.tar.gz" | cut -f1)"
fi

step "10. 清单与校验（没有 sha256 的备份等于没有备份）"
if [ "$DRY_RUN" = "1" ]; then
    log "（dry-run）跳过 MANIFEST 生成"
else
    : >"$MANIFEST"
    while IFS= read -r f; do
        printf '%s  %s\n' "$(sha_of "$f")" "$(basename "$f")" >>"$MANIFEST"
    done < <(find "$DIR" -maxdepth 1 -type f ! -name 'MANIFEST.sha256' ! -name 'BACKUP.log' ! -name 'INFO.txt' | sort)
    ok "MANIFEST.sha256 共 $(wc -l <"$MANIFEST") 条"
    chmod 640 "$DIR"/* 2>/dev/null || true

    echo
    echo "  ---- 产物清单 ----"
    du -h "$DIR"/* | sort -h
    echo
    echo "  ---- sha256 ----"
    sed 's/^/  /' "$MANIFEST"
    echo
    printf '%s备份完成：%s%s\n' "$C_GRN" "$DIR" "$C_RST"
    echo "回滚用： rollback.sh --backup $(basename "$DIR")"
fi
