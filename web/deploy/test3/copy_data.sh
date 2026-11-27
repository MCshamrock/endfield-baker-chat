#!/usr/bin/env bash
# test3 预演站 —— 迁移建表之后，把 3 张基础表从生产库拷过来（root）。
#
# 只拷 characters / worldbook / api_keys：
#   · characters 没有它前端列表是空的，没法测
#   · worldbook  角色设定的世界书
#   · api_keys   上游 key（加密过，需要同一把 BAKER_SECRET 才解得开）
# **users / devices / conversations / messages 一张都不拷** ——
# 预演站上不该出现任何真实用户的聊天记录。
set -euo pipefail
DB=baker_back_test3
PROD_DB=baker_back

for t in characters worldbook api_keys; do
    n=$(mysql -N -e "SELECT COUNT(*) FROM \`$PROD_DB\`.\`$t\`")
    echo "   生产 $t = $n"
done

mysqldump --replace --no-create-info --skip-add-locks --skip-comments "$PROD_DB" characters worldbook api_keys | mysql "$DB"

echo "--- 拷贝后"
for t in characters worldbook api_keys; do
    n=$(mysql -N -e "SELECT COUNT(*) FROM \`$DB\`.\`$t\`")
    echo "   test3 $t = $n"
done
echo "--- 确认没有用户数据"
mysql -N -e "SELECT CONCAT('users=', (SELECT COUNT(*) FROM \`$DB\`.users), ' conversations=', (SELECT COUNT(*) FROM \`$DB\`.conversations), ' messages=', (SELECT COUNT(*) FROM \`$DB\`.messages))"
