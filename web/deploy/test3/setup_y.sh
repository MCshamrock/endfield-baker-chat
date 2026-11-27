#!/usr/bin/env bash
# test3.peilika.beer —— 预演站（新前端 + 新后端，数据完全隔离）
#
# 这一步是「以 y 身份跑」的部分：代码、.env、目录、迁移、systemd 用户单元。
# 数据库建库与授权、nginx 站点在 setup_root.sh 里（需要 root）。
set -euo pipefail

BASE=/home/y/baker-v4-test3
DB=baker_back_test3
PROD_ENV=/home/y/baker-v4/back/.env
VENV=/home/y/baker-v4/venv

# 从生产 .env 里取数据库口令与密钥（同一台机器，不落盘不外传）
PW=$(grep -E '^BAKER_DATABASE_URL=' "$PROD_ENV" | sed -E 's#.*baker:([^@]+)@.*#\1#')
SECRET_LINE=$(grep -E '^BAKER_SECRET=' "$PROD_ENV")

echo "== 目录"
mkdir -p "$BASE/logs" "$BASE/_runtime/uploads" "$BASE/_runtime/tmp"
rm -rf "$BASE/back"
mkdir -p "$BASE/back"
tar xzf /home/y/tmp/test3-back.tgz -C "$BASE/back"
echo "   back: $(find "$BASE/back" -name '*.py' | wc -l) 个 py 文件"

echo "== .env（端口 5661，库 $DB，密钥与生产同一把：api_keys 才解得开）"
cat > "$BASE/back/.env" <<EOF
# test3 预演站配置（与生产隔离：自己的库、自己的端口）
# 600；含密钥，绝不进 git。
BAKER_DATABASE_URL=mysql+pymysql://baker:${PW}@127.0.0.1:3306/${DB}?charset=utf8mb4
BAKER_HOST=127.0.0.1
BAKER_PORT=5661
BAKER_LOG_LEVEL=INFO

BAKER_API_VERSION=v1
BAKER_SCHEMA_VERSION=1
BAKER_APP_MIN=3.0.0
BAKER_PRODUCT=lite
BAKER_GEN_MODE=inline

BAKER_UPSTREAM_BASE_URL=https://apihub.agnes-ai.com/v1
BAKER_UPSTREAM_MODEL=agnes-2.5-flash
BAKER_UPSTREAM_API_KEY=
BAKER_UPSTREAM_TIMEOUT=60
BAKER_UPSTREAM_MAX_TOKENS=2048
BAKER_UPSTREAM_TEMPERATURE=0.8

${SECRET_LINE}
BAKER_UPLOAD_DIR=${BASE}/_runtime/uploads
BAKER_TMP_DIR=${BASE}/_runtime/tmp

BAKER_WORLDBOOK_ENABLED=true
BAKER_WORLDBOOK_SLUG=endfield
BAKER_USER_RPM=30
BAKER_KEY_WAIT_SECONDS=20
BAKER_UPLOAD_TTL_HOURS=24
EOF
chmod 600 "$BASE/back/.env"
echo "   .env 写好（$(stat -c %a "$BASE/back/.env")）"
