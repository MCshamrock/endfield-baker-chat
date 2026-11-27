#!/usr/bin/env bash
# rollback.sh —— 把入口切回老后端 / 把网页换回备份。目标：**3 分钟内回到 02:00 之前的状态**。
#
# 为什么回滚这么短：
#   * 切 nginx 时**没有杀**老 perlica-* 进程（31 个单元一直在跑），所以"回滚后端"
#     只是把 upstream 的端口换回去 + reload，不需要重新启动 40 个进程。
#   * 新后端独立在 5660 + 独立库表，回滚时停掉它就行，老后端根本不读它。
#
# 数据库为什么**不回滚**：
#   * 老后端的状态不在 MySQL 里（会话在进程内存 + run/group_sessions.db，配置在 JSON）；
#     新库 baker_back 对老后端完全不可见 ⇒ 留着不阻碍回滚，反而保住这段时间的新数据。
#   * 迁移 001–019 全是**加表/加列/加索引**，没有删改；真要回退，用备份里的
#     mysql-baker_back.sql.gz 整个倒（会丢这期间的新消息，所以只在"数据被写坏"时才用）。
#
# 用法（root；先跑 --dry-run 看清单）：
#   bash rollback.sh --dry-run
#   bash rollback.sh                        # nginx 回到切换前 + 网页换回备份
#   bash rollback.sh --nginx-only           # 只回 nginx（网页不动）
#   bash rollback.sh --web-host prod        # 只把正式站网页换回备份
#   bash rollback.sh --stop-new             # 顺手把新后端停掉（彻底放弃这次上线）
#   bash rollback.sh --backup 20261118_010203_deploy  # 指定用哪次备份（默认自动找最新）
#
# 参数：
#   --nginx-only / --web-host prod|staging|both
#   --backup NAME      /opt/bakerai-backup 下的目录名（默认最新一个）
#   --state FILE       默认 /opt/bakerai-backup/state/nginx-last.txt
#   --stop-new         停 baker-api（不影响老后端）
#   --dry-run          只打印将执行的命令
#   --yes              跳过确认（自动化/演练用）

SCRIPT_NAME=rollback
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"

NGINX_ONLY=0
WEB_HOST=both
BACKUP_NAME=""
STATE_FILE=/opt/bakerai-backup/state/nginx-last.txt
STOP_NEW=0
ASSUME_YES=0
BACKUP_ROOT=/opt/bakerai-backup
WEB_PROD=/var/www/peilika-chat
WEB_STAGE=/var/www/peilika-v2

while [ $# -gt 0 ]; do
    case "$1" in
        --nginx-only) NGINX_ONLY=1; shift ;;
        --web-host) WEB_HOST="${2:?}"; shift 2 ;;
        --backup) BACKUP_NAME="${2:?}"; shift 2 ;;
        --state) STATE_FILE="${2:?}"; shift 2 ;;
        --stop-new) STOP_NEW=1; shift ;;
        --backup-root) BACKUP_ROOT="${2:?}"; shift 2 ;;
        --web-prod) WEB_PROD="${2:?}"; shift 2 ;;
        --web-stage) WEB_STAGE="${2:?}"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

require_root
need_cmd nginx curl systemctl tar

# 找备份目录（默认最新的 *_deploy）
# ⚠️ 没有 /opt 备份目录也必须能回滚 nginx —— 2026-11-18 01:36 实测过这个坑：
#    切换失败时 rollback.sh 因为"找不到 backup.sh 的产物"直接 die，结果什么也没还原。
#    nginx 的还原只依赖 state 文件里登记的 .bak_bakerai_*，跟 /opt 备份无关。
if [ -z "$BACKUP_NAME" ]; then
    BACKUP_NAME="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '*_deploy' -printf '%f\n' 2>/dev/null | sort | tail -1)"
fi
if [ -n "$BACKUP_NAME" ] && [ -d "$BACKUP_ROOT/$BACKUP_NAME" ]; then
    B="$BACKUP_ROOT/$BACKUP_NAME"
    log "使用备份：$B（$(du -sh "$B" 2>/dev/null | cut -f1)）"
else
    B=""
    warn "没找到 $BACKUP_ROOT/*_deploy 备份：只能还原 nginx，网页 dist 无法还原（--nginx-only 场景够用）"
fi

if [ "$ASSUME_YES" = "0" ] && [ "$DRY_RUN" = "0" ]; then
    warn "即将执行回滚：nginx 恢复到切换前、网页换回备份里的 dist。"
    printf '  确认继续？输入 yes 回车：'
    read -r ans
    [ "$ans" = "yes" ] || die "已取消（没动任何东西）"
fi

FAIL=0

# ---------------------------------------------------------------- 1. nginx
step "1. nginx：恢复切换前的 vhost（upstream 回到 5500/5810）"
if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE"   # 提供 ts / web / api / file= / backup= 列表
    log "状态文件：$STATE_FILE（ts=${ts:-?} web=${web:-?} api=${api:-?}）"
fi

# 收集要还原的文件：优先用状态文件里的 backup=；没有就扫 *.bak_bakerai_*
declare -a PAIRS=()
if [ -f "$STATE_FILE" ] && grep -q '^backup=' "$STATE_FILE"; then
    while IFS= read -r line; do
        case "$line" in
            backup=*)
                bak="${line#backup=}"
                if [ -f "$bak" ]; then
                    orig="${bak%%.bak_bakerai_*}"
                    PAIRS+=("$bak|$orig")
                else
                    warn "备份文件不在了：$bak"
                fi
                ;;
        esac
    done <"$STATE_FILE"
else
    warn "没有状态文件，退回到扫 *.bak_bakerai_* 的备份（每个文件只取最新那份）"
    declare -A SEEN=()
    while IFS= read -r bak; do
        orig="${bak%%.bak_bakerai_*}"
        [ -n "${SEEN[$orig]:-}" ] && continue   # 同一文件有多份备份时，只保留第一份（已按时间倒序）
        SEEN[$orig]=1
        PAIRS+=("$bak|$orig")
    done < <(ls -1t /etc/nginx/sites-available/*.bak_bakerai_* 2>/dev/null | head -10)
fi

if [ "${#PAIRS[@]}" -eq 0 ]; then
    warn "没有找到任何 nginx 备份 —— 只能靠 sed 把 5660 换回 5500"
    for f in /etc/nginx/sites-available/api-peilika /etc/nginx/sites-available/api2-peilika; do
        [ -f "$f" ] || continue
        log "  \$ sed -i 's/127.0.0.1:5660/127.0.0.1:5500/' $f"
        if [ "$DRY_RUN" = "0" ]; then
            cp -a "$f" "$f.bak_rollback_$(stamp)"
            sed -i -E 's#127\.0\.0\.1:5660#127.0.0.1:5500#' "$f"
        fi
        PAIRS+=("|$f")
    done
fi

for pair in "${PAIRS[@]}"; do
    bak="${pair%%|*}"; orig="${pair##*|}"
    if [ -z "$bak" ]; then
        log "  (sed 已处理) $orig"
        continue
    fi
    log "  \$ cp -a $bak $orig"
    if [ "$DRY_RUN" = "0" ]; then
        cp -a "$bak" "$orig" && ok "已还原 $(basename "$orig")"
    fi
done

if [ "$DRY_RUN" = "0" ]; then
    if nginx -t 2>&1 | sed 's/^/  /'; then
        systemctl reload nginx && ok "nginx reload 完成（入口已回老 gateway）"
    else
        die "还原后 nginx -t 仍失败！手动处理：cp -a <备份> <文件> 后 nginx -t"
    fi
else
    log "  \$ nginx -t && systemctl reload nginx"
fi

# ---------------------------------------------------------------- 2. 老后端
step "2. 老后端：确认 perlica-* 还在跑（切换时没杀，理论上不用动）"
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
# root 下 `systemctl --user` 连不到 y 的 user bus（会永远数出 0，误报"服务没起来"）。
# 用 sudo -u y 切回真正的属主再数 —— 这也是 rollback 在 -Sudo 下必须能跑对的原因。
if is_root; then
    ACTIVE="$(sudo -u y env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user list-units 'perlica-*' --no-legend --no-pager 2>/dev/null | grep -c ' running ' || true)"
else
    ACTIVE="$(systemctl --user list-units 'perlica-*' --no-legend --no-pager 2>/dev/null | grep -c ' running ' || true)"
fi
log "perlica-* 正在运行的单元数：${ACTIVE:-0}"
if [ "${ACTIVE:-0}" -lt 20 ]; then
    warn "少于 20 个，可能有单元没起来。尝试重启网关与群聊（不动 chat/prompt 实例）"
    if [ "$DRY_RUN" = "0" ]; then
        systemctl --user restart perlica-gateway perlica-group 2>/dev/null || warn "重启失败，手工看 systemctl --user status perlica-gateway"
    fi
else
    ok "老后端进程都在，不需要重启（这正是回滚快的原因）"
fi

if [ "$STOP_NEW" = "1" ]; then
    step "2b. 停掉新后端（--stop-new）"
    log "  \$ systemctl --user disable --now baker-api"
    if [ "$DRY_RUN" = "0" ]; then
        systemctl --user disable --now baker-api 2>/dev/null || warn "baker-api 没在跑/没安装"
        ok "baker-api 已停（库与文件保留，随时可以再起）"
    fi
fi

# ---------------------------------------------------------------- 3. 网页 dist
if [ "$NGINX_ONLY" = "1" ]; then
    warn "--nginx-only：网页 dist 不动"
else
    step "3. 网页 dist：换回备份里的那一份"
    # 还原 dist：优先用「目录型备份」（rsync 快、无需解包），其次 tar.gz。
    # 候选名覆盖两种来源：backup_web.sh（web-v2 / web-chat）与 backup.sh（web-*-dist.tar.gz）。
    restore_dist() {
        local key="$1" root="$2"
        local dir_cand tgz_cand alt_tgz
        case "$key" in
            prod)          dir_cand="$B/web-chat"; tgz_cand="$B/web-chat.tar.gz"; alt_tgz="$B/web-prod-dist.tar.gz" ;;
            test2|staging) dir_cand="$B/web-v2";   tgz_cand="$B/web-v2.tar.gz";   alt_tgz="$B/web-stage-dist.tar.gz" ;;
            *) warn "未知的 web 目标：$key"; return 0 ;;
        esac
        if [ -z "${B:-}" ]; then warn "没有备份目录，跳过 $root"; return 0; fi

        if [ -d "$dir_cand" ]; then
            log "  \$ rsync -a --delete --exclude=.well-known/ $dir_cand/ $root/"
            if [ "$DRY_RUN" = "0" ]; then
                rsync -a --delete --exclude='.well-known/' "$dir_cand/" "$root/"
                chown -R www-data:www-data "$root" 2>/dev/null || true
                ok "$root 已还原（index.html $(sha_of "$root/index.html" | cut -c1-16)…）"
            fi
            return 0
        fi

        local tgz=""
        [ -f "$tgz_cand" ] && tgz="$tgz_cand"
        [ -z "$tgz" ] && [ -f "$alt_tgz" ] && tgz="$alt_tgz"
        if [ -z "$tgz" ]; then warn "没有 $key 的 dist 备份（找过 $dir_cand / $tgz_cand / $alt_tgz）"; return 0; fi

        log "  \$ tar -xzf $tgz → 覆盖 $root"
        if [ "$DRY_RUN" = "1" ]; then return 0; fi
        local tmpd; tmpd="$(mktemp -d)"
        tar -xzf "$tgz" -C "$tmpd"
        local src="$tmpd/$(basename "$root")"
        if [ ! -d "$src" ]; then warn "包里没有 $(basename "$root")"; rm -rf "$tmpd"; return 0; fi
        rsync -a --delete --exclude='.well-known/' "$src/" "$root/"
        chown -R www-data:www-data "$root" 2>/dev/null || true
        rm -rf "$tmpd"
        ok "$root 已还原（index.html $(sha_of "$root/index.html" | cut -c1-16)…）"
    }
    case "$WEB_HOST" in
        prod|chat|B)          restore_dist prod  "$WEB_PROD" ;;
        test2|staging|A)      restore_dist test2 "$WEB_STAGE" ;;
        both)                 restore_dist prod  "$WEB_PROD"; restore_dist test2 "$WEB_STAGE" ;;
        *) die "--web-host 只能是 test2|prod|both（staging/chat 是别名）" ;;
    esac
fi

# ---------------------------------------------------------------- 4. 验证
step "4. 验证：老形状 /chat 必须 200，且 /health 回到 gateway_service"
if [ "$DRY_RUN" = "1" ]; then
    log "  \$ curl -H 'Host: api.peilika.beer' http://127.0.0.1/health"
    exit 0
fi

H="$(curl -sS -m 10 -H 'Host: api.peilika.beer' http://127.0.0.1/health 2>/dev/null || true)"
echo "  /health → ${H:0:140}"
case "$H" in
    *gateway_service*) ok "入口已回到老 gateway" ;;
    *baker-backend*)   warn "仍是新后端！检查 $STATE_FILE 里的 file= 列表是否都还原了"; FAIL=$((FAIL+1)) ;;
    *)                 warn "响应形状不认识"; FAIL=$((FAIL+1)) ;;
esac

CODE="$(curl -sS -m 30 -o "$HTTP_BODY_FILE" -w '%{http_code}' -X POST \
    -H 'Host: api.peilika.beer' -H 'Content-Type: application/json' \
    --data-binary '{"character_id":"perlica","message":"","history":[]}' \
    http://127.0.0.1/chat 2>/dev/null || echo 000)"
if [ "$CODE" = "200" ] || [ "$CODE" = "400" ] || [ "$CODE" = "422" ]; then
    ok "/chat 通了（HTTP $CODE；老网关对空消息就是这个反应）"
else
    warn "/chat 返回 $CODE：$(head -c 120 "$HTTP_BODY_FILE")"
    FAIL=$((FAIL+1))
fi

echo
echo "  ---- 数据库：按设计不回滚 ----"
echo "  baker_back 保留（新后端的数据都在这里；老后端根本不读它）"
echo "  真要连数据一起退： gunzip -c $B/mysql-baker_back.sql.gz | mysql -uroot baker_back"
echo "  注意：那会丢掉回滚时点之后新写入的消息，只在"数据被写坏"时用。"

if [ "$FAIL" -gt 0 ]; then
    die "回滚完成但有 $FAIL 项验证没通过，请人工检查（证据见上）"
fi
printf '\n%s回滚完成，入口已回到 02:00 前的状态%s\n' "$C_GRN" "$C_RST"
echo "  备份仍保留在：$B"
echo "  新后端数据保留在：/home/y/baker-v4（没删，随时可再切）"
