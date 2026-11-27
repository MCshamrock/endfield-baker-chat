#!/usr/bin/env bash
# test3 预演站 —— 建表（迁移）+ systemd 用户单元 + 起服务。
# 跑在 setup_root.sh 之后（库和授权已经就绪）。
set -euo pipefail

BASE="${BASE:-$HOME/baker-v4-test3}"
DB=baker_back_test3
VENV="${VENV:-$HOME/baker-v4/venv}"
URL="mysql+pymysql://baker:$(grep -E '^BAKER_DATABASE_URL=' "$BASE/back/.env" | sed -E 's#.*baker:([^@]+)@.*#\1#')@127.0.0.1:3306/${DB}?charset=utf8mb4"

echo "== 建表（app.migrate.apply_all）"
cd "$BASE/back"
"$VENV/bin/python" - <<PY
from app.migrate import apply_all
apply_all("${URL}")
print("   migrations ok")
PY

echo "== systemd 用户单元 baker-api-test3"
cat > ~/.config/systemd/user/baker-api-test3.service <<UNIT
# baker-api-test3.service —— 预演站后端（5661），与生产的 baker-api(5660) 完全隔离。
# 代码在 ~/baker-v4-test3/back，库 baker_back_test3，日志各自一份。
[Unit]
Description=BakerAI backend TEST3 (baker-api-test3, 127.0.0.1:5661)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BASE/back
EnvironmentFile=-$BASE/back/.env
ExecStart=$VENV/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 5661 \
    --log-level info \
    --no-server-header \
    --timeout-keep-alive 65
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=20
StandardOutput=append:$BASE/logs/baker-api-test3.log
StandardError=append:$BASE/logs/baker-api-test3.err.log
ProtectSystem=false
ProtectHome=false

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now baker-api-test3.service
sleep 4
echo "== 状态"
systemctl --user is-active baker-api-test3.service
echo "== readyz"
curl -s -m 5 http://127.0.0.1:5661/readyz; echo
echo "== health"
curl -s -m 5 http://127.0.0.1:5661/health; echo
echo "== 新接口（owner-data，无 token 应 401）"
curl -s -o /dev/null -w '  /v1/auth/owner-data -> %{http_code}\n' -m 5 http://127.0.0.1:5661/v1/auth/owner-data
