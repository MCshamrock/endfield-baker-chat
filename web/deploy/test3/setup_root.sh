#!/usr/bin/env bash
# test3 预演站 —— 需要 root 的部分：建库 / 授权 / 拷基础数据 / 装 nginx 站点。
#
# 数据隔离策略：
#   · 只从生产库拷 3 张「不该重造」的基础表：characters（角色）、worldbook（世界书）、
#     api_keys（上游 key，加密过，解密需要同一把 BAKER_SECRET）。
#   · **users / devices / conversations / messages 一张都不拷** ——
#     预演站上不该出现真实用户的聊天记录，测登录墙也不需要。
set -euo pipefail

DB=baker_back_test3
PROD_DB=baker_back
BASE=/home/y/baker-v4-test3

echo "== 建库"
mysql -e "CREATE DATABASE IF NOT EXISTS \`$DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
mysql -e "GRANT ALL PRIVILEGES ON \`$DB\`.* TO 'baker'@'localhost'; FLUSH PRIVILEGES;"
echo "   $DB 就绪"

echo "== 拷基础表放在建表之后做（见 copy_data.sh）"
echo "   原因：--no-create-info 只灌数据，表得先由迁移建出来"

echo "== 前端 dist"
mkdir -p /var/www/peilika-chat-test3
rm -rf /var/www/peilika-chat-test3/*
tar xzf /home/y/tmp/test3-dist.tgz -C /var/www/peilika-chat-test3
chown -R www-data:www-data /var/www/peilika-chat-test3
echo "   $(ls /var/www/peilika-chat-test3 | tr '\n' ' ')"
echo "   index 引用的 js: $(grep -o 'assets/index-[A-Za-z0-9_-]*\.js' /var/www/peilika-chat-test3/index.html | head -1)"

echo "== nginx 站点"
cat > /etc/nginx/sites-available/test3-peilika <<'NGINX'
# test3.peilika.beer —— 预演站（新前端 + 新后端 5661，数据独立）
#
# 与 chat.peilika.beer 的差别只有三处：server_name / root / 反代端口。
# 反代**不** include 公共片段 bakerai-new-api.conf：那份写死 5660（生产），
# 预演站必须打自己的 5661，否则测的就是生产后端。
server {
    listen 80;
    server_name test3.peilika.beer;

    root /var/www/peilika-chat-test3;
    index index.html;

    client_max_body_size 64m;

    # ---- /v1/* 反代到预演后端 5661 ----
    location /v1/ {
        proxy_pass http://127.0.0.1:5661;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering     off;
        proxy_cache         off;
        proxy_read_timeout  300s;
        proxy_send_timeout  300s;
    }

    # ---- /group/* 同上（网页 vhost 自带，不走公共片段）----
    location /group/ {
        proxy_pass http://127.0.0.1:5661/group/;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering     off;
        proxy_read_timeout  300s;
        proxy_send_timeout  300s;
    }

    location = /readyz {
        proxy_pass http://127.0.0.1:5661/readyz;
    }

    # index.html 绝不缓存：预演站会反复换构建
    location = /index.html {
        add_header Cache-Control "no-cache, no-store, must-revalidate" always;
        add_header Pragma "no-cache" always;
        expires -1;
    }
    location ~* \.html$ {
        add_header Cache-Control "no-cache, must-revalidate";
    }

    location /assets/ {
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/test3-peilika /etc/nginx/sites-enabled/test3-peilika
nginx -t
systemctl reload nginx
echo "   nginx reloaded"
