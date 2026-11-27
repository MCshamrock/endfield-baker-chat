#!/usr/bin/env bash
# install_mysql.sh —— **只在确认没装 MySQL 时用**。装 8.4 + 初始化 + 建库建号。
#
# 现状（2026-11-18 实测）：服务器 **已经装了** mysql-server 8.4.13（apt），
#   systemd 单元 enabled+running，bind 127.0.0.1:3306，库 baker_back / baker_back_test 都在，
#   账号 baker 只对这两个库 ALL PRIVILEGES，root 走 auth_socket（`sudo mysql` 免密）。
#   ⇒ 2 点这一版**不需要跑这个脚本**。留着是为了"机器重装/换机"那天。
#
# 跑之前它会先自检：已经装了就直接退出 0（幂等），不会把现网库搞掉。
#
# 用法：
#   pwsh -File bakeraiSSH/scripts/remote.ps1 -SshHost baker -ScriptFile <本脚本> -Sudo
# 参数：
#   --db NAME            要建的库（默认 baker_back；会同时建 <NAME>_test）
#   --user NAME          应用账号（默认 baker）
#   --password-file PATH 密码来源文件（默认 /home/y/baker-v4/.db_password）
#                        文件不存在就生成一个 32 位随机密码并写进去（600，属主 y）
#   --app-dir DIR        新后端的目录（默认 /home/y/baker-v4），用来决定密码文件放哪、
#                        以及顺手把 BAKER_DATABASE_URL 写进 .env（只在 .env 不存在时）
#   --dry-run            只打印将执行的命令
#
# 硬规矩：**密码不许硬编码**，也不打印到屏幕/日志里（只打印"已写入哪个文件"）。

SCRIPT_NAME=install_mysql
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

DB_NAME=baker_back
DB_USER=baker
APP_DIR=/home/y/baker-v4
PASSWORD_FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --db) DB_NAME="${2:?}"; shift 2 ;;
        --user) DB_USER="${2:?}"; shift 2 ;;
        --password-file) PASSWORD_FILE="${2:?}"; shift 2 ;;
        --app-dir) APP_DIR="${2:?}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

[ -n "$PASSWORD_FILE" ] || PASSWORD_FILE="$APP_DIR/.db_password"

require_root
need_cmd apt-get systemctl mysql

step "1. 自检：是不是已经装过了（装过就退出，绝不覆盖现网库）"
MYSQLD_BIN="$(command -v mysqld || true)"
MYSQL_VER=""
if [ -n "$MYSQLD_BIN" ]; then
    MYSQL_VER="$(mysqld --version 2>/dev/null || true)"
    ok "已安装：$MYSQL_VER"
    systemctl is-active --quiet mysql && ok "mysql.service 正在运行" || warn "mysql.service 没在跑（可能需要 systemctl start mysql）"
    if mysql -uroot -N -e 'select 1' >/dev/null 2>&1 || mysql -uroot -N -e 'select 1' >/dev/null 2>&1; then
        log "root 能连上；已存在的库："
        mysql -uroot -N -e 'show databases' 2>/dev/null | grep -E '^baker' | sed 's/^/    /' || true
    fi
    step "结论：不需要安装。要建新库请直接用 SQL（见 README §T-24h 第 2 步）"
    exit 0
fi

step "2. 预计耗时与风险（装之前先看清楚）"
cat <<'TXT'
  apt 源：cn.archive.ubuntu.com（本机实测可用；2026-11-15 那次装成功了）
  下载体积：约 30 MB（mysql-server + core + client）
  预计耗时：1–3 分钟（含 apt update）
  风险：
    * 若 apt 源不通 → 换清华/阿里镜像（/etc/apt/sources.list.d/ubuntu.sources 改 URIs）
    * 装完 mysql 默认 bind 127.0.0.1，不会把 3306 暴露到公网（符合预期，别改）
    * root 默认 auth_socket（`sudo mysql` 免密），**不要**改成密码登录
    * 若 datadir 已有旧数据会被沿用；全新机器会自动 initialize（apt 的 postinst 做）
TXT
if [ "$DRY_RUN" = "1" ]; then
    warn "dry-run：下面这些命令不会真的执行"
fi

step "3. apt 安装"
run apt-get update
run env DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server
run systemctl enable --now mysql
if [ "$DRY_RUN" = "0" ]; then
    for _ in $(seq 1 30); do
        mysql -uroot -N -e 'select 1' >/dev/null 2>&1 && break
        sleep 1
    done
    mysql -uroot -N -e 'select version()' >/dev/null 2>&1 \
        || die "装完了但 root 连不上，先看 journalctl -u mysql -n 50"
    ok "MySQL 起来了：$(mysql -uroot -N -e 'select version()')"
fi

step "4. 密码（不硬编码：从文件读，没有就生成）"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ [ -f $PASSWORD_FILE ] || openssl rand -hex 16 > $PASSWORD_FILE (chmod 600)"
    DB_PASS="<dry-run>"
else
    if [ ! -s "$PASSWORD_FILE" ]; then
        mkdir -p "$(dirname "$PASSWORD_FILE")"
        (umask 077; openssl rand -hex 16 >"$PASSWORD_FILE")
        chown "$(stat -c %u:%g "$(dirname "$PASSWORD_FILE")")" "$PASSWORD_FILE" 2>/dev/null || true
        chmod 600 "$PASSWORD_FILE"
        ok "已生成随机密码 → $PASSWORD_FILE（600，不打印内容）"
    else
        ok "沿用已有密码文件：$PASSWORD_FILE"
    fi
    DB_PASS="$(cat "$PASSWORD_FILE")"
fi

step "5. 建库建号（utf8mb4 / utf8mb4_0900_ai_ci，与老库一致）"
for db in "$DB_NAME" "${DB_NAME}_test"; do
    if [ "$DRY_RUN" = "1" ]; then
        log "  \$ CREATE DATABASE IF NOT EXISTS $db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    else
        mysql -uroot -e "CREATE DATABASE IF NOT EXISTS \`$db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
        mysql -uroot -e "GRANT ALL PRIVILEGES ON \`$db\`.* TO '$DB_USER'@'localhost'"
        ok "库 $db 就绪，已授权给 $DB_USER@localhost"
    fi
done
[ "$DRY_RUN" = "0" ] && mysql -uroot -e "FLUSH PRIVILEGES"

# 账号密码：CREATE USER 幂等写法（存在则只改密码）
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY <从 $PASSWORD_FILE 读取>"
    log "  \$ ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY <...>"
else
    mysql -uroot -e "CREATE USER IF NOT EXISTS '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS'"
    mysql -uroot -e "ALTER USER '$DB_USER'@'localhost' IDENTIFIED BY '$DB_PASS'"
    ok "账号 $DB_USER@localhost 就绪（密码来自 $PASSWORD_FILE）"
fi

step "6. 验证（用应用账号真连一次，不看 rc=0）"
if [ "$DRY_RUN" = "0" ]; then
    mysql -h127.0.0.1 -P3306 -u"$DB_USER" -p"$DB_PASS" -N -e "select current_user(), @@version" "$DB_NAME" 2>/dev/null \
        | sed 's/^/    /' || die "应用账号连不上，检查密码/授权"
    ok "应用账号连通 $DB_NAME"
    echo
    echo "  下一步（把连接串写进新后端 .env，由 deploy_backend.sh 自动做）："
    echo "    BAKER_DATABASE_URL=mysql+pymysql://${DB_USER}:<从 ${PASSWORD_FILE} 读取>@127.0.0.1:3306/${DB_NAME}?charset=utf8mb4"
fi

printf '%sinstall_mysql 完成%s\n' "$C_GRN" "$C_RST"
