# BakerAI 上线运行手册（2026-11-18 02:00 停机部署）

> 目标：把**新后端**（`BakerAI_new/web/back`，FastAPI + MySQL，5660）和**新 web**（`BakerAI_new/web/front` 的 dist）
> 切到线上，老后端（`perlica-*`，5500/5810）**保持运行不杀**做回滚底。
>
> 本文所有"现状"数字都是 **2026-11-18 00:31–00:45 在 `ssh.peilika.beer` 上只读实测**的，不是推断。
> 作业标准见 skill `bakerai-remote-maintenance`（只读体检 → 备份 → 变更 → 验证 → 回滚）。

---

## 0. 一页结论

### 8 项预检红黄绿灯

| # | 项 | 灯 | 一句话 |
|---|---|---|---|
| 1 | Python 3.14 | 🟢 | `/usr/bin/python3.14` = **3.14.4**（系统级，`python3` 也指它）；venv 已有 |
| 2 | MySQL | 🟢 | **已装 8.4.13**（apt，11-15 装的），active，`127.0.0.1:3306`，`baker_back` 库 + `baker` 账号都在 ⇒ **不用装** |
| 3 | 磁盘/内存 | 🟢 | `/` 余 **25 G**，数据盘余 253 G，内存可用 **29.5 G** |
| 4 | 服务拓扑 | 🟢 | `perlica-*` 31 个 + `baker-test-*` 13 个全是 systemd 用户单元；5660 现在被影子 `baker-v4` 占着 |
| 5 | nginx | 🟢 | 1.28.3，`nginx -t` 通过；**TLS 在 Cloudflare，本机无证书**（"证书确认"= 看浏览器锁头） |
| 6 | cloudflared | 🟢 | 生产隧道 `*.peilika.beer → localhost:80`；**新路由全在 nginx 做，隧道不用动** |
| 7 | 数据资产 | 🟢 | api_pool **47 个 key（25 启用）**、characters 38 文件、characters_v2 38、worldview 3 KB、群聊 SQLite 32 K+4 MB WAL、世界书源 md 141 KB（已随包） |
| 8 | 防火墙/5660 | 🟢 | ufw 不活动；5660/3306 只绑 `127.0.0.1` ⇒ **不需要对外开**（nginx 同机反代） |

> ### 🚫 不能切 nginx 的红线（任一不满足 → 不切，老 gateway 继续跑）
> ```
> /instances  keys.total=47 且 keys.enabled=25
> /readyz     migrations=019_prompt_variant
> worldbook=147   characters=36   character_prompts=72（v1 36 + v2 36）
> smoke.sh --generate 必须真出一条话（reply 非空）
> ```
> 只看 `/readyz` 200 不算验过 —— 它只证明"库通、迁移跑完"，证明不了"能出话"。

**结论：02:00 具备，但有 3 个前置**（见 §2，全部可在 01:30 前做完，预计 45–60 分钟）。

### 其它关键判断

| 项 | 结论 |
|---|---|
| 后端能不能起 | 能。Python 3.14.4、MySQL 8.4.13、`baker_back` 库、venv、systemd 用户单元**都已在位**（11-15 的影子服务） |
| web 能不能上 | 能。`dist` 已构建（8.8 MB / 95 assets），两块 dist 都**没有 Service Worker**（不用清 SW） |
| 真正停机时长 | **≈ 0**（老 gateway 不停；后端是"换 nginx upstream"，用户可见中断只有 reload 的那一下） |
| 唯一不可逆的东西 | **web 覆盖**（老 dist 会被换掉）→ 必须先备份、且保留 `rollback.sh` |

---

## 1. 预检基线（8 项，全部有命令输出为证）

### 1.1 Python —— 🟢 绿

```bash
$ python3 -V; ls /usr/bin/python3.14 /usr/local/lib/python3.14 2>/dev/null
Python 3.14.4
/usr/bin/python3.14
/usr/local/lib/python3.14
$ /home/y/baker-v4/venv/bin/python -V
Python 3.14.4                     # readlink → /usr/bin/python3.14
```

* **系统级就有 3.14**（不是只有 `other/venv`）：`/usr/bin/python3.14`，`python3` 也指向它；**没有 3.12/3.13**。
* 影子服务 venv 已在 `/home/y/baker-v4/venv`（80 MB，3.14.4），依赖与 `requirements.lock` 基本对齐。
* ⚠️ **缺 2 个运行时包**：`cryptography`（key 加密）、`python-multipart`（上传）。`pip install -r requirements.lock` 会补上，**必须在 T-24h 做**。

### 1.2 MySQL —— 🟢 绿（已装，不用装）

```bash
$ dpkg -l | grep -E '^ii +mysql'
mysql-server   8.4.13-0ubuntu0.26.04.2
$ systemctl status mysql --no-pager | head -3
Active: active (running) since Sun 2026-11-15 18:17:40 CST; 2 days ago
$ sudo mysql -N -e 'show databases'
baker_back
baker_back_test
$ sudo mysql -N -e "show grants for 'baker'@'localhost'"
GRANT ALL PRIVILEGES ON `baker_back`.*      TO `baker`@`localhost`
GRANT ALL PRIVILEGES ON `baker_back_test`.* TO `baker`@`localhost`
```

| 项 | 现状 |
|---|---|
| 版本 | **8.4.13**（apt，Ubuntu 26.04 官方源） |
| 监听 | `127.0.0.1:3306`（**不对公网**，正确） |
| root | `auth_socket` → `sudo mysql` 免密；**不要再改成密码登录** |
| 应用账号 | `baker@localhost`（密码 32 位，在 `/home/y/baker-v4/.db_password`，600） |
| 库 | `baker_back`、`baker_back_test` 都在，各 **0.6 MB / 14 表**，utf8mb4_0900_ai_ci |
| 参数 | `max_connections=151`、`innodb_buffer_pool=128M`、`sql_mode` 含 STRICT —— 够用（真实负载 2.4 次/分钟） |
| 磁盘 | datadir `/var/lib/mysql`（205 MB） |

**⇒ `install_mysql.sh` 这一版不用跑**（脚本会自检并直接退出 0，留着给换机那天）。

### 1.3 磁盘 / 内存 —— 🟢 绿

```
/dev/sda2  116G  86G  25G  78%  /            ← 余 25 G
/dev/sdb3  469G 211G 253G  46%  /run/media/y/游戏   ← 余 253 G
内存 55 G（可用 29.5 G）｜swap 4 G（已用 1.9 G）｜40 线程，load 5.3
```

预留估算（这一版）：

| 用途 | 需要 | 说明 |
|---|---|---|
| 新后端代码 + venv | ~90 MB | `/home/y/baker-v4` |
| MySQL 增量（36 角色 + 147 世界书 + 47 key + 用户消息） | < 100 MB | 现有库才 0.6 MB，种子后预计 3–5 MB，之后按消息增长 |
| 上传/取件码 | **上限 2 GB**（`BAKER_UPLOAD_TOTAL_BYTES`），TTL 24h | 老客户端 60 MB/包；到顶会回 507，跑 `scripts/cleanup_uploads.py` |
| 备份（本次） | ~60–80 MB | `/opt/bakerai-backup` 在 `/` 上 → 25 G 余量足够 |
| 日志 | 已配 logrotate（baker-v4 → 将加 baker-api，日轮转 7 份 / 50 M） | 老 `baker/backend/logs` 3.8 G 是历史包袱，不在本次范围 |

### 1.4 现有服务拓扑 —— 🟢 绿（全部是 systemd 用户单元，没有脚本拉起的散进程）

`systemctl --user list-units 'perlica-*' 'baker-test-*'`：

| 端口 | 进程 / 单元 | 归属 | 本次处置 |
|---|---|---|---|
| 5500 | `perlica-gateway.service` | 系统用户单元 | **不动**（回滚底） |
| 5501 | `baker-test-gateway.service` | 测试 | 不动 |
| 5601–5608 | `perlica-prompt@{5601..5608}.service`（8 个） | 正式提示词 | 不动 |
| 5611–5614 | `baker-test-prompt@{5611..5614}.service` | 测试 | 不动 |
| 5701–5716 | `perlica-chat@{5701..5716}.service`（16 个） | 正式单聊 | 不动 |
| 5721–5724 | `baker-test-chat@{5721..5724}.service` | 测试 | 不动 |
| 5810 | `perlica-group.service` | 正式群聊 | 不动 |
| 5820 | `baker-test-group.service` | 测试群聊 | 不动 |
| 5910 | `baker-test-apipool.service` | 测试密钥池 | 不动 |
| 5921 / 5922 | `rag-gpu0.service` / `rag-gpu1.service` | RAG 双卡 | 不动 |
| 5900 / 5930 / 5940 / 5960 | `message-hub` / `baker-localdata` / `baker-monitor` / `baker-survey` | 辅助服务 | 不动 |
| 8200 / 8210 | `perlica-legacy-proxy` / `perlica-legacy-key` | 老 key 服务 | 不动 |
| **5660** | **`baker-v4.service`（影子，跑的是 11-15 的旧代码）** | 新后端 | **T-0 停掉，换 `baker-api.service`** |
| 80 | nginx 1.28.3 | 入口 | T+15 `reload` |
| 3080 / 23504 / 6099 | `dsh-bridge` / 1Panel / napcat | 无关 | 不动 |

* 合计 71 个监听端口；`perlica-*` 31 个单元、`baker-test-*` 13 个，其余是桌面会话服务（本机是"桌面机兼生产"）。
* 老 `/instances` 当前还活着：`5500/health` → `{"status":"ok","service":"gateway_service",...}`（回滚时用它证明"真的回去了"）。

### 1.5 nginx —— 🟢 绿（TLS 在 Cloudflare，本机没有证书）

* 版本 1.28.3；`sites-enabled` 22 个站点；`nginx -t` **当前合法**；`client_max_body_size 20m`（全局，web vhost 需覆盖成 64m）。
* **没有任何 `ssl_certificate`，也没有 `/etc/letsencrypt`** ⇒ 本机只监听 80，TLS 由 Cloudflare 终结。
  ⇒ **"证书确认"这一项 = 确认 CF 边缘证书有效**（浏览器打开 `https://chat.peilika.beer` 看锁头即可），本机无事可做。

| vhost | root / upstream | 本次是否改 |
|---|---|---|
| `api.peilika.beer` | upstream `perlica_gateway`→**5500**、`perlica_group`→**5810**；有 `/chat`、`/chat/stream`（SSE 关了缓冲）、`/health`、`/instances`、`/group/`、`/group-health` | **改：upstream 指向 5660 + 补 include** |
| `api2.peilika.beer` | 同上（`perlica_gateway2`/`perlica_group2`） | **改：同上** |
| `chat.peilika.beer` | root `/var/www/peilika-chat`；有 `location = /api/legacy-key → 127.0.0.1:8210` | **改：插 include（同源 API）+ 64m；`/api/legacy-key` 保持原样** |
| `test2.peilika.beer` | root `/var/www/peilika-v2`（预演站） | **改：同上（A 预演用）** |
| `local.peilika.beer` | → `127.0.0.1:5930`（老客户端云同步中转，`proxy_read_timeout 120s`） | **本次不动**（2 点不切；详见风险 3） |
| `notice.` / `android.` / `windows.` / `peilika.beer` / `baker.` / `demo.` / `preview` / `legacy.` / `message.` / `monitor.` / `rag.` / `survey.` / `ui-v3.` / `dsh.` / `panel.` | 各自静态或反代 | 不动 |

**CORS 是谁加的**：新后端自己在 `app/main.py` 的 `CORSMiddleware`（`allow_origins=["*"]`、`allow_credentials=False`，鉴权走 Bearer token）。
nginx 上的 `add_header Access-Control-Allow-Origin` 只出现在 `chat/_legacy/assets` 这类**静态资源**和 `la/notice` 上，与 API 无关 ⇒ **切后端不用动 CORS**。

### 1.6 cloudflared —— 🟢 绿（不需要动隧道）

```
cloudflared.service      生产隧道 peilika   → /etc/cloudflared/config.yml
cloudflared-ssh.service  SSH 隧道 ssh-tunnel → config-ssh.yml（ssh.peilika.beer → sshd:22）
（另有 mefrpc.service 与用户级 endgala-web.yml，与本次无关）
```

生产隧道 ingress 关键两条：

```yaml
- hostname: "*.peilika.beer"   # 所有子域通配
  service: http://localhost:80
- hostname: peilika.beer
  service: http://localhost:80
```

⇒ **新增/改路由一律在 nginx 里做，不碰隧道**（`*.peilika.beer` 已经全指到 80）。改 ingress 是"全站 502"级别的事故，本次禁止。

### 1.7 现有数据资产 —— 🟢 绿（都在，且新后端种子够用）

| 资产 | 路径 | 实测 |
|---|---|---|
| **api_pool.json** | `/run/media/y/游戏/baker/backend/api_pool.json` | **47 个 key，其中 25 个 enabled**；`defaults={base_url,model,rpm,window_s,max_concurrency,cooldown_fail_base,fail_limit}`；600 |
| 角色卡 v1 | `…/backend/characters/` | **38 个文件**（36 张卡 + index.json），688 KB |
| 角色卡 v2（长卡） | `…/backend/characters_v2/` | **38 个文件**，916 KB ⇒ 新后端 `import_prompts_v2.py --source` 直接指这里 |
| 世界观 | `…/backend/worldview.txt` | 3,032 B（已进 `schema/004_seed_worldview.sql`，不用再导） |
| 群聊 SQLite | `…/backend/run/group_sessions.db` | 32 KB **+ 4.1 MB WAL**（备份必须用 sqlite 在线备份，不能裸 cp） |
| 实例表 | `…/backend/instance_config.json` | 6,483 B（网关每 15 s 刷新；新后端不需要它，`/instances` 自己生成） |
| 世界书 source.md | `F:\dshworkspace\character_cardsrknights-endfield-cards-neo\世界书\终末地世界书_通用版_v1.0.md`（141 KB） | **权威源**，已随包放进 `deploy/seed/`（同名）；`import_worldbook.py` 实测解析 **147 条 / 新建 147**。另附一份 `worldbook_endfield.sql` 作为无 md 时的退路（同一批数据，REPLACE INTO 幂等） |
| 老 web dist | `/var/www/peilika-chat`（chat）｜`/var/www/peilika-v2`（test2） | 7.8 MB / 8.7 MB，各 95 assets，**均无 SW / workbox / manifest** |

### 1.8 防火墙 / 端口 —— 🟢 绿（**5660 不需要对外开**）

```
ufw status → 状态：不活动（iptables 只有 Docker 链，无 INPUT 限制）
ss -lntH | grep -E ':(5660|3306)' → 127.0.0.1:5660 / 127.0.0.1:3306
```

* `5660` 只绑 `127.0.0.1`，nginx 同机反代 ⇒ **公网不需要开、也不该开**。
* 局域网调试（真机 APK 直连）需要临时 `BAKER_HOST=0.0.0.0` + 无防火墙 → **本次不做**（ufw 不活动，一旦绑 0.0.0.0 等于全公网裸奔）。
* ⚠️ 这台机器 `/var/run/reboot-required` 存在（内核/库待重启）—— **02:00 窗口里不要重启机器**，等上线稳定后另约。

---

## 1.9 当前状态（2026-11-18 01:40：**后端已切**，web/local 未切）

| 项 | 状态 |
|---|---|
| 最终版 `.env` | ✅ 已写（`BAKER_SECRET` 48 hex **已固定**，`UPLOAD_DIR` 指 `/home/y/baker-v4/_runtime/uploads`，600）—— **之后不许再改** |
| 代码 | ✅ `/home/y/baker-v4/back`（来自 `/tmp/bakerai-stage.tgz`，sha256 `0c2af94e…`） |
| 依赖 | ✅ 运行时 26 包（从 lock 过滤掉 mypy/ruff/pytest），走了 `/tmp/bakerai-wheels` 离线装，**9 秒** |
| 库 | ✅ `migrations=019_prompt_variant`（19 个）、`keys 47/25`、`characters 36`、`prompts v1 36 + v2 36`、`worldbook 147`、21 表 |
| 临时实例验收 | ✅ 在 **5661** 起临时进程跑 `smoke`：**12 PASS / 0 FAIL / 1 SKIP**（未跑 `--generate`，按计划留到 02:00） |
| 影子服务 | ✅ `baker-v4` **没有被碰**（pid 2965021 仍在 5660），`baker-api.service` **未安装**、nginx **未动** |
| 切换结果 | ✅ **2026-11-18 01:36:34 已切 api + api2**：两边 `/health`=`baker-backend`、`/instances`=47/25、真 `/chat` 200 有回复；web/test2/`local.` **未动** |
| 02:00 窗口内只剩 | `deploy_backend.sh --start` → `smoke.sh --generate` → `switch_nginx.sh --web both --api yes`（预计 10 分钟内） |

> 说明：`--stage` 第一次跑停在一条自检 SQL（`table_schema=databases()` 写错，MySQL 没这个函数），
> 已改成 `DATABASE()`；第二次 6 秒跑完。**失败时它确实停住了、没有继续往下写种子** —— 断言生效。

### 1.10 nginx include 里到底有哪些 location（模板 `nginx/bakerai-new-api.conf`）

| location | 作用 | 今晚用得上? |
|---|---|---|
| `/v1/` | 新契约（SSE 关缓冲、600 s、64m） | ✅ 新 web / 新客户端 |
| `/v1` | 一小段 JSON，避免落到 SPA 假 200 | ✅ |
| `/chat` | 老单聊（无状态） | ✅ 老客户端 |
| `/chat/stream` | 老流式（关缓冲、600 s、gzip off） | ✅ |
| `/chat/suggestions` | 老输入推荐 | ✅ |
| `/group/` | 老群聊（建群/发言/推荐/关群） | ✅ |
| `/instances` | 实例与 **keys 健康度** | ✅ 运维/客户端判活 |
| `/group-health` | 老监控脚本探活（→ 新后端 `/health`） | ✅ |
| `/health` | 进程活着（api 系 vhost 本来也有，这里给网页域补一条） | ✅ |
| `/readyz` | 库通 + 迁移版本 | ✅ 部署判定 |
| `/upload` | 老云同步收包（64m / 300 s） | ⏳ 配了但今晚不切 `local.` |
| `/fetch` | 老云同步取包（300 s） | ⏳ 同上 |

✅ 已用 `/tmp/nginx-test` 副本 + `nginx -t` 验过：**没有 duplicate location 冲突**（exact 与 prefix 混用是合法的）。

## 2. 三个最大风险（按"会不会让 2 点翻车"排序）

### 风险 1（最高）：新后端第一次真跑 → 迁移/种子/依赖任何一个不对，就等于没有后端可切

* 为什么危险：现在的 5660 是 **11-15 的影子服务**，库只跑到 `002_seed`（3 个角色、0 个 key、无世界书表）。
  要变成"生产可切"需要：装 2 个依赖 → 跑 17 个迁移 → 导 47 key → 导 147 世界书 → 导 36 套 v2 长卡。
  任何一步失败，**02:00 就会出现"nginx 切过去 502 / 角色是旧的 / 没有 key 出不了话"**。
* 规避动作（全部在 T-24h 做完，且**不碰线上流量**）：
  1. 依赖先装并在服务器上**导入一次** `cryptography`/`multipart` 验证（`deploy_backend.sh` 步骤 3 会自检）；
  2. 迁移 + 种子**在影子库上先跑一遍**（就是 `baker_back`，没有真实用户数据，坏了可以直接 drop 重建）；
  3. 每个阶段有**数字断言**（迁移=019、keys=47、worldbook=147、v2≥36、characters=36），不满足就退出非 0；
  4. 决定性验收：`smoke.sh --generate` 真出一条话。**没出过话 = 没验过**。
* 兜底：老 gateway 一直在跑；`rollback.sh` 把 upstream 换回 5500（实测 `/health` 会变回 `gateway_service`）。

### 风险 2：web 覆盖后"用户看到旧页面 / 白屏"，且 localStorage 迁移失败

* 为什么危险：`chat.peilika.beer` 是**同源迁移**的唯一正确位置（老用户的 localStorage/IndexedDB 就在这个 origin）。
  覆盖 `/var/www/peilika-chat` 后，如果新包有路径/缓存问题，用户侧表现是白屏或"数据没了"，而且**没有 SW 可以清**——
  也就是说不能靠"清缓存"救，只能靠 dist 回滚。
* 规避动作：
  1. **先 A 后 B**：新 dist 先上 `test2.peilika.beer`（`/var/www/peilika-v2`），走完首页/迁移提示条/账号页冒烟，再覆盖 `chat`；
  2. 覆盖前 `backup.sh` 必跑（web-prod-dist.tar.gz），覆盖用 `rsync -a --delete`（旧 assets 不残留）；
  3. 覆盖后立刻核对 `index.html` 的 **sha256 与构建产物一致**（不是"看文件时间"）；
  4. `index.html` 已经带 `no-cache, must-revalidate`，`/assets/` 带内容哈希 ⇒ 正常情况下不需要用户清缓存；
  5. 若白屏：`rollback.sh --web-host prod` 30 秒换回。
* 注：新旧是**两条不同构建线**（老 `index-DcpIHeqc.js` 24017 B；新 `index-DB6uyW05.js` 6978 B），不是同一份代码的小改，
  所以"看起来只是换了个包"是错觉，必须按上面 5 条走。

### 风险 3：大包上传撞 Cloudflare 100 秒 / nginx 超时 → 老客户端"云同步"失败

* 为什么危险：老客户端导出包**最大 60 MB**，走 `/upload`。CF 对响应有 ~100 s 限制；
  线上 `local.peilika.beer` 的 `proxy_read_timeout` 只有 120 s（已发现的历史缺陷）。
  慢网/大包时 nginx 或 CF 先断，客户端只看到"取回失败"，用户会以为数据丢了。
* 规避动作：
  1. 新 vhost 的 `/upload` `/fetch` 一律 `client_max_body_size 64m` + `proxy_read_timeout/send_timeout 300s`（模板已写死）；
  2. **切换那天实测一次 60 MB 包**（见 §3 T+55）；
  3. `local.peilika.beer` 本次不切；要切的那天必须把它的 120 s 一起改掉（已记在 §6 待办）；
  4. 兜底：新后端 `/upload` 是流式落盘，不会把 60 MB 读进内存；失败也不会脏数据（ticket 只在成功后建）。

---

## 3. 时间轴（照着敲）

> 通道约定（全篇通用）：
> ```powershell
> $WS = 'F:\dshworkspace\BakerAI\bakeraiSSH'
> pwsh -File "$WS\scripts\remote.ps1" -SshHost baker -ScriptFile <脚本> [-Sudo]
> pwsh -File "$WS\scripts\remote_put.ps1" -Local <本地> -Remote <远端> [-Sudo] [-Owner www-data:www-data -Mode 644]
> ```
> `-SshHost baker` = ME Frp 快通道（~1 s），Cloudflare 那条（8–17 s）留作备胎。
> 本文脚本都在 `F:\dshworkspace\BakerAI\BakerAI_new\deploy\`。

### T-24h（现在 → 01:45，预计 45–60 分钟，全部在"影子"上做，不影响用户）

#### ① 备份（5 分钟，**第一步，不许跳**）

```bash
# 服务器侧（root）
bash /tmp/deploy/backup.sh --tag predeploy
```
**预期**：`/opt/bakerai-backup/20261118_HHMMSS_predeploy/`，里面有
`old-backend-code.tar.gz / new-backend-code.tar.gz / nginx-config.tar.gz / api_pool.json(600) /
group_sessions.db / web-prod-dist.tar.gz / web-stage-dist.tar.gz / mysql-baker_back.sql.gz /
configs.tar.gz / MANIFEST.sha256 / INFO.txt`，最后打印一串 sha256。
**失败怎么办**：
* `tar: ... file changed as we read it` → 正常（日志在写），只要 sha256 有输出就继续；
* `mysqldump` 报 `Access denied` → 脚本用 `mysql -uroot`（auth_socket）必须 root 跑，检查是不是忘了 `-Sudo`；
* 磁盘不足（`No space left`）→ `/` 还有 25 G，不该发生；先 `du -sh /opt/bakerai-backup` 清旧备份。

> ⚠️ 这一步同时是**回滚的唯一依据**。没跑备份就不许往下走。

#### ② 数据库确认（3 分钟，不用装）

```bash
sudo mysql -N -e "select version(); show databases; show grants for 'baker'@'localhost'"
```
**预期**：`8.4.13...`、`baker_back`、`baker_back_test`、两条 GRANT。
**失败怎么办**：`baker_back` 不在 → `sudo mysql -e "CREATE DATABASE baker_back CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"`；
`baker` 账号不在 → 跑 `install_mysql.sh`（会补建账号，密码写 `/home/y/baker-v4/.db_password`）。

#### ③ 全新库 6 步（lead 2026-11-18 本地实测两遍，**顺序不能变**）

> 服务器上 `baker_back` 已经建好（11-15 影子那次），所以 1) 通常跳过；但换机/重建库时按这个来。
> **最容易错的是第 2 步和第 5 步**：先有 `BAKER_SECRET` 才能导 key；不导世界书，检索等于没有。

```bash
# 1) 建库 + 建号（utf8mb4 / utf8mb4_0900_ai_ci）
sudo mysql -e "CREATE DATABASE IF NOT EXISTS baker_back CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
               GRANT ALL PRIVILEGES ON baker_back.* TO 'baker'@'localhost'; FLUSH PRIVILEGES;"

# 2) 先写 .env 的 BAKER_SECRET（**必须是最终值，之后不许改**）
#    key 用 AES-256-GCM 加密存库；换 secret = 已存的 key 全部解不开 = 池子归零。
#    deploy_backend.sh 会在 .env 缺失时随机生成 24 字节 hex（`openssl rand -hex 24`）。

# 3) 迁移（19 个，001…019，顺序正确才算过）
.venv/bin/python scripts/migrate.py
#    实测：执行了 19 个迁移 … 最后一条 019_prompt_variant

# 4) 导 key（用老的 api_pool.json，47 个）
.venv/bin/python scripts/import_keys.py --file /run/media/y/游戏/baker/backend/api_pool.json
#    实测：47 个 key（25 启用 / 22 禁用；agnes-ai.cn 7 个、apihub 40 个），AES-256-GCM

# 5) 世界书（**不跑这步 worldbook 表是 0 条**）
.venv/bin/python scripts/import_worldbook.py     --file "deploy/seed/终末地世界书_通用版_v1.0.md" --world endfield
#    实测：解析 147 条 / 新建 147

# 6) v2 长卡（可选，但老客户端 use_new_prompt 会用到）
.venv/bin/python scripts/import_prompts_v2.py --source /run/media/y/游戏/baker/backend/characters_v2
#    实测：36 条 v2（v1 211,615 字 / v2 299,366 字）
```

**装完的自检基线（服务器必须与本地一致）**：

| 检查 | 期望值 |
|---|---|
| `GET /readyz` | `{"ok":true,"db":"mysql","schema":"1","migrations":"019_prompt_variant"}` |
| `GET /instances` | `keys {total:47, enabled:25, cooldown:0, disabled:22}` |
| `POST /upload`（raw JSON） | 200 + 老形状（`code/bytes/uploaded_at/cleanup_at/keep_hours`；`uploaded_at` 是 **CST**） |
| `POST /fetch` | 与上传**逐字节相同**（不 re-serialize、不加 BOM） |
| `POST /chat` | 200 且 `reply` 非空 |
| `GET /v1/meta` | `capabilities.web_search = false` |
| 库里 | `characters=36`、`character_prompts=36(v1)+36(v2)`、`worldbook=147`、`api_keys=47` |

> 三条配套规矩：
> 1. `import_keys.py` 读的是 `.env` 的 `BAKER_SECRET` ⇒ **.env 必须先落地**（部署脚本已把这一步排在第 2 位，不是按字母序）；
> 2. 上传目录必须用 `BAKER_UPLOAD_DIR` 指到持久盘（默认会算到 `/home/y/_runtime`，那是项目外；本包生成 `.env` 时已设成 `/home/y/baker-v4/_runtime/uploads`），取件包 24h 自动清，磁盘至少留 5 G；
> 3. 起服务只用 venv 里的解释器（`/home/y/baker-v4/venv/bin/uvicorn`），`--host 127.0.0.1 --port 5660`，**别 0.0.0.0**（前面有 nginx）。

#### ③ 传代码 + 装依赖（15–20 分钟）

```powershell
# Windows：把后端源码打包（排除 venv/缓存），推到 /tmp
cd F:\dshworkspace\BakerAI\BakerAI_new\web
tar -czf $env:TEMP\back.tgz --exclude='back/.venv*' --exclude='back/__pycache__' --exclude='back/_runtime' --exclude='back/.env' back
pwsh -File $WS\scripts\remote_put.ps1 -Local $env:TEMP\back.tgz -Remote /tmp/back.tgz
```
```bash
# 服务器：解包到暂存区，跑部署（会 rsync → 装依赖 → 迁移 → 种子 → 装 unit）
mkdir -p /tmp/bakerai-stage && tar -xzf /tmp/back.tgz -C /tmp/bakerai-stage
```
> 注意：`deploy_backend.sh` 要连 `deploy/` 目录一起用（它读 `systemd/`、`seed/`、`lib/`）。
> 最省事：**把整个 `BakerAI_new/deploy/` 也传上去**：
> ```powershell
> tar -czf $env:TEMP\deploy.tgz -C F:\dshworkspace\BakerAI\BakerAI_new deploy
> pwsh -File $WS\scripts\remote_put.ps1 -Local $env:TEMP\deploy.tgz -Remote /tmp/deploy.tgz
> ```
> ```bash
> mkdir -p /tmp/deploy && tar -xzf /tmp/deploy.tgz -C /tmp --strip-components=0
> ```

先**空跑**看它准备做什么（不改任何东西）：

```bash
bash /tmp/deploy/deploy_backend.sh --src /tmp/bakerai-stage/back --dry-run
```
**预期**：逐步打印 `$ rsync … / $ /usr/bin/python3.14 -m venv … / $ python scripts/migrate.py …`，最后 `(dry-run) 未写入`。
**失败怎么办**：`缺 /usr/bin/python3.14` → 机器变了，先查（`ls /usr/bin/python3*`）；`$PASS_FILE 缺失` → 回到 ②。

> ⚠️ **pip 实测很慢**：2026-11-18 00:43 在服务器上 `pip download cryptography==50.0.2`（4.8 MB）
> 用了 **5 分 58 秒**（13.5 kB/s）。别在 02:00 才开始装。T-24h 先下到本地目录，再离线装：
> ```bash
> mkdir -p /tmp/bakerai-wheels
> /home/y/baker-v4/venv/bin/pip download -d /tmp/bakerai-wheels >     cryptography==50.0.2 python-multipart==0.0.33
> # 之后 deploy_backend.sh 的 pip install 会优先用本地 wheel（其余包已经装好了，不会再去下）
> ```

然后真跑（**先不重启**，避免 5660 被占）：

```bash
# 两阶段（推荐）：--stage 只铺路（代码/依赖/迁移/种子），不点火
bash /tmp/deploy/deploy_backend.sh --src /tmp/bakerai-stage/back --stage
# 窗口里只需要点火（--start 只装 unit + 起服务 + 健康断言，秒级）
bash /tmp/deploy/deploy_backend.sh --start
```
**预期**（关键行）：
```
✓ python3.14 = Python 3.14.4
✓ 数据库连通：baker_back，当前迁移版本 002_seed
✓ 关键依赖可导入：fastapi uvicorn sqlalchemy pymysql pydantic_settings cryptography multipart
执行了 17 个迁移：003_seed_prompts, …, 019_prompt_variant
✓ 最后迁移 = 019_prompt_variant，表数 = 21
导入 47 个 key（其中 25 个启用），加密方式 AES-256-GCM
  https://api.agnes-ai.cn/v1: 7
  https://apihub.agnes-ai.com/v1: 40
✓ api_keys: 总数 47 / 启用 25（期望总数 47）
✓ worldbook: 147 条（期望 147）
✓ characters=36，v2 提示词=36（期望 ≥36）
✓ baker-api 已 enable
--stage：不重启，服务仍是旧代码
```

> ✅ **--stage 的判定线（唯一有效的一条）**：
> ```bash
> bash /tmp/deploy/deploy_backend.sh --start     # 先点火（秒级）
> bash /tmp/deploy/smoke.sh --generate           # 必须 12/12 PASS，且 /chat 那条 reply 非空
> ```
> `--stage` 之后再跑 `smoke.sh --generate` **真调一次上游**：只有它同时证明
> 「key 池能借到 key」+「上游可达」+「提示词/角色/世界书拼得对」。
> **不许只看 `/readyz` 就宣布后端 OK。**
**失败怎么办**：
| 现象 | 原因 / 处置 |
|---|---|
| `pip install` 卡住/超时 | PyPI 慢（历史上全量 889 s）。只差 2 个包，重跑一次；仍慢就 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 再跑 |
| `迁移没跑到 019` | 看 migrate 输出第一行报错；最常见是 `baker` 只有 baker_back 的权限（库名写错） |
| `key 数量不对（0 ≠ 47）` | `BAKER_SECRET` 没写进 .env（脚本会提示）；或 `api_pool.json` 路径变了 |
| `worldbook: 0 条` | 种子文件没传上去（`/tmp/deploy/seed/worldbook_endfield.sql`） |
| `v2 提示词少于 36` | `--characters-v2` 路径不对（默认 `/run/media/y/游戏/baker/backend/characters_v2`） |

#### ④ 预演 nginx 切换（5 分钟，只打印 diff）

```bash
bash /tmp/deploy/switch_nginx.sh --dry-run --web both
```
**预期**：打印 4 个文件的 `diff -u`，每块只有两类改动——
① upstream 里 `server 127.0.0.1:5500;` → `:5660;`（api、api2 各 1 行；`:5810;` 同样换成 5660）；
② `server_name …;` 后插入 `client_max_body_size 64m;` + `include /etc/nginx/snippets/bakerai-new-api.conf;`。
最后 `(dry-run) 未 reload`。
**失败怎么办**：diff 里出现**其它行**变化 → 说明模板不匹配线上文件，停止，人工看 diff（不要 `cp` 覆盖）。

#### ⑤ 证书确认（2 分钟）

浏览器开 `https://chat.peilika.beer`、`https://api.peilika.beer/health`，看锁头 + 证书签发者 = Cloudflare。
**失败怎么办**：CF 边缘证书是自动的，若报错先查 Cloudflare 面板（本机没有证书可修）。

#### ⑥ 真客户端兼容产物确认（同事 `compat-harness` 的产出）

```bash
ls -la /f/dshworkspace/BakerAI/BakerAI_new/local-client/*.exe
ls -la /f/dshworkspace/BakerAI/deploy/out/*.apk
```
**预期**：`EndfieldBakerChat-compat.exe` 与某个 `BakerAI-3.2.x*.apk` 存在（目前有 `BakerAI-3.2.4-rc.apk`）。
**失败怎么办**：没有可用产物 → T+60 的"真客户端抽检"降级为"网页版 + 同一台机器的 EXE 旧版"，
并明确记录"客户端兼容性未验收"（这是**上线后**的风险，不阻塞 02:00 的后端切换）。

---

### T-0（02:00）停机：只停影子服务，老 gateway 不动

**顺序写死，不许跳步**（`baker-api` 绑不上 5660 就是 502，`Restart=always` 还会无限重试）：

```bash
# ① 停影子 + 禁止它自启（否则下次开机又抢 5660）
systemctl --user disable --now baker-v4
# ② 确认端口真的释放（必须"没有输出"）
ss -lntH | grep ':5660' || echo "5660 已释放"
# ③ 起新后端（这一条内部会再查一次端口，占了就拒绝启动）
bash /tmp/deploy-new/deploy/deploy_backend.sh --start
# ④ 就绪探针（migrations 必须是 019_prompt_variant）
curl -sS http://127.0.0.1:5660/readyz; echo
# ⑤ 老 gateway 还活着（回滚底，别停它）
curl -sS http://127.0.0.1:5500/health
```
**预期**：`5660 已释放`；老 gateway 回 `{"status":"ok","service":"gateway_service",...}`。
**失败怎么办**：
* `5660` 还被占 → `ss -lntp | grep 5660` 找 pid；不是 baker-v4 就 `ps -o args= -p <pid>` 看清楚再动（**不要盲杀**）；
* 老 gateway 挂了 → **立刻中止上线**，先 `bash /run/media/y/游戏/baker/tools/start_all.sh` 把老服务救回来（这时还没有任何切换，用户无感）。

> 说明：本方案是**蓝绿切换**，用户可见中断只有 T+15 的 `nginx reload`（毫秒级）。
> 如果你要的是"停 5 分钟做维护页"，那是另一套（需要维护页 + 停 5500），本次**不建议**——
> 老 gateway 停掉就失去了 30 秒回滚的能力。

---

### T+5（02:05）起新后端

```bash
systemctl --user start baker-api
sleep 2
curl -sS http://127.0.0.1:5660/health;  echo
curl -sS http://127.0.0.1:5660/readyz;  echo
curl -sS http://127.0.0.1:5660/instances | python3 -m json.tool | head -30
curl -sS http://127.0.0.1:5660/v1/meta; echo
systemctl --user status baker-api --no-pager | head -12
```
**成功判据**：
| 检查 | 通过标准 |
|---|---|
| `/health` | `{"status":"ok","service":"baker-backend"}` |
| `/readyz` | `{"ok":true,"db":"mysql","schema":"1","migrations":"019_prompt_variant"}` |
| `/instances` → `keys` | `total = 47`、`enabled = 25`（**用户说的"47"是总数**；启用数由 api_pool 里的 enabled 决定，实测 25） |
| `/instances` → `modules.chat[0].status` | `healthy`；`mode = inline` |
| `/v1/meta` | `caps` 含 `group/login/memory/continue/think`；`capabilities.web_search = false` |
| `systemctl --user status` | `Active: active (running)`，`Restart=always` 生效 |

**失败怎么办**：
```bash
journalctl --user -u baker-api -n 60 --no-pager     # 先看日志
```
| 日志里的关键词 | 处置 |
|---|---|
| `Address already in use` | 5660 没释放（回到 T-0 第 2 步） |
| `ModuleNotFoundError: cryptography` | 依赖没装成功 → 重跑 `deploy_backend.sh --src ... --skip-seed`（只补依赖 + 重启） |
| `Access denied for user 'baker'` | `.db_password` 与 MySQL 里的密码不一致 → 用 `install_mysql.sh` 的 ALTER 逻辑重设（会同步写文件） |
| `Table 'baker_back.xxx' doesn't exist` | 迁移没跑全 → `venv/bin/python scripts/migrate.py` 单独跑，看报错 |
| `readyz` 503 但 `/health` 200 | 库连不上/迁移没跑 —— **不要切 nginx**，修好再继续 |

---

### T+15（02:15）切 nginx

**推荐顺序：先预演站（A），再正式站（B），api 最后一起切。**

```bash
# ① 只切预演站 test2（新后端接管它的 /v1 /chat /group …）
bash /tmp/deploy/switch_nginx.sh --web staging --api no
```
**成功判据**（脚本自己会打）：
```
✓ api.peilika.beer /health …（--api no 时跳过）
✓ test2.peilika.beer / → 200
✓ test2.peilika.beer /v1/meta → 200
```
然后用浏览器/curl 打开 `https://test2.peilika.beer/` → 新首页 + 账号页正常。

```bash
# ② 切正式站 + **api 与 api2 两条线路一起切**（客户端有主/备线路切换，
#    只切 api 的话用户一切备用线路就打到旧服务）。这一步才是"切后端"。
#    脚本有红线门禁：/health=baker-backend、/readyz=019、/instances keys 47/25，
#    再加"必须已跑过 smoke --generate"的人工确认（--smoke-ok），任一不满足就拒绝切。
bash /tmp/deploy/switch_nginx.sh --web both --api yes --smoke-ok
```
**预期**：
```
---- diff: /etc/nginx/sites-available/api-peilika ----
-    server 127.0.0.1:5500;
+    server 127.0.0.1:5660;
…
✓ nginx -t 通过
✓ nginx reload 完成
✓ api.peilika.beer /health 200 …
✓ api.peilika.beer /instances 200 …
✓ chat.peilika.beer / 200 …
api.peilika.beer/health → {"status":"ok","service":"baker-backend"}
✓ 确认已在跑新后端
```
**失败怎么办**：
* `nginx -t` 失败 → 脚本会**自动还原**刚改的文件并退出（什么都没生效）；
* 验证项失败（例如 502）→ `bash /tmp/deploy/rollback.sh --yes --nginx-only` 先回入口，再查日志；
* **不要**在此时重启老 gateway，也不要动隧道。

> ⚠️ 铁律：这一步**不杀** `perlica-*`（31 个单元）。它们空转也没关系 —— 回滚就靠它们。

---

### T+25（02:25）冒烟（全套，任一失败非 0）

```bash
# 走 nginx（带 Host 头，绕过 Cloudflare），并顺带测网页同源
bash /tmp/deploy/smoke.sh --base http://127.0.0.1 --host api.peilika.beer \
     --web https://chat.peilika.beer --generate
```
**成功判据**（`合计：PASS 13  FAIL 0` 之类，FAIL 必须为 0）：
| # | 项 | 判据 |
|---|---|---|
| 1 | `GET /health` | 200 且 `status=ok` |
| 2 | `GET /readyz` | 200 且 `migrations=019_*` |
| 3 | `GET /instances` | `keys.total=47`、`enabled≥1`、`chat=healthy` |
| 4 | `GET /v1/meta` | caps 含 group/login/memory/continue，`web_search=false` |
| 5 | `POST /v1/auth/device` | 201 + 拿到 token |
| 6 | `GET /v1/characters` | 200 且 ≥36 个角色 |
| 7 | `POST /chat`（老形状，`--generate`） | 200 且 `reply` 非空（**这一条是"真的能出话"的唯一证据**） |
| 8 | `POST /group/session` | 200 + session_id；`/group/sessions` 200；`/group/session/close` 200 |
| 9 | `POST /upload` → `POST /fetch` | 200 + 取件码，且**取回的字节与上传一致** |
| 10 | `GET {web}/` | 200 且 HTML 里有 `assets/` |
| 11 | `GET {web}/v1/meta` | 200（证明同源反代生效） |

**单条 curl（给不想跑脚本的时候用）**：
```bash
# 老形状 /chat（不花钱的连通性检查：空 message 应回 400 + 统一错误体）
curl -sS -i -H 'Host: api.peilika.beer' -H 'Content-Type: application/json' \
  -d '{"character_id":"perlica","message":"","history":[]}' http://127.0.0.1/chat | head -20
# 老形状 /instances（看 keys）
curl -sS -H 'Host: api.peilika.beer' http://127.0.0.1/instances | python3 -m json.tool | head -40
# /v1 设备注册
curl -sS -i -H 'Host: chat.peilika.beer' -H 'Content-Type: application/json' \
  -d '{"platform":"win","app_version":"3.2.2"}' http://127.0.0.1/v1/auth/device | head -20
# /upload → /fetch 往返
CODE=$(curl -sS -H 'Host: api.peilika.beer' -H 'Content-Type: application/json' \
  -d '{"probe":1}' http://127.0.0.1/upload | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')
echo "code=$CODE"; curl -sS -H 'Host: api.peilika.beer' -H 'Content-Type: application/json' \
  -d "{\"code\":\"$CODE\"}" http://127.0.0.1/fetch; echo
```
**失败怎么办**：按失败项定位——
| 失败项 | 先查 |
|---|---|
| `/chat` 404 | include 没生效（`nginx -T | grep -c bakerai-new-api`） |
| `/chat` 502 | 5660 没起 / upstream 拼错 |
| `/chat` 200 但 `reply` 空 | key 池全是冷却/禁用（`/instances` 看 `keys.cooldown/disabled`）、上游 429 |
| `/v1/meta` 404 | 该 vhost 没插 include（`--web` 漏了） |
| `/upload` 413 | `client_max_body_size` 没生效（看该 server 块） |
| `/fetch` 404 | 取件码不存在（上传那步没成功） |
| `/v1/characters` < 36 | 种子没灌（回到 T-24h ③） |

---

### T+35（02:35）部署 web（先 A 预演，再 B 正式）

```bash
# ① 预演站 test2：把新 dist 铺到 /var/www/peilika-v2
#    （本地构建：cd BakerAI_new/web/front && npm run build → dist/）
#    先把 dist 传上去：
```
```powershell
cd F:\dshworkspace\BakerAI\BakerAI_new\web\front
tar -czf $env:TEMP\dist.tgz -C dist .
pwsh -File $WS\scripts\remote_put.ps1 -Local $env:TEMP\dist.tgz -Remote /tmp/dist.tgz
```
```bash
# ② 服务器：解包 + 覆盖（先备份已在 T-24h 做过；这里再确认一次）
ls -la /opt/bakerai-backup/*_predeploy/web-stage-dist.tar.gz   # 必须有
rm -rf /tmp/newdist && mkdir -p /tmp/newdist && tar -xzf /tmp/dist.tgz -C /tmp/newdist
rsync -a --delete /tmp/newdist/ /var/www/peilika-v2/
chown -R www-data:www-data /var/www/peilika-v2
sha256sum /var/www/peilika-v2/index.html /f/... 2>/dev/null || true
```
**成功判据**：
* `sha256sum /var/www/peilika-v2/index.html` == 本地 `dist/index.html` 的 sha256；
* `https://test2.peilika.beer/` 出**新首页**；`/v1/meta` 200；
* 账号页（注册/登录）能打开；迁移提示条出现（前端逻辑，若没有提示条属产品预期差异，记录即可）；
* 浏览器 Console 无报错（重点看有没有 `Failed to fetch`、CORS、404 assets）。

```bash
# ③ 正式站：同一份 dist 覆盖 /var/www/peilika-chat（**这一步才影响所有用户**）
rsync -a --delete /tmp/newdist/ /var/www/peilika-chat/
chown -R www-data:www-data /var/www/peilika-chat
```
**关于缓存/SW**：两块老 dist **都没有 Service Worker**（`sw.js`/`workbox-*`/`manifest.json` 全部不存在，已实测），
所以**不需要"清 SW"**；`index.html` 本身 `no-cache`、assets 带内容哈希 ⇒ 正常刷新即可。
若个别用户仍看到旧页面：让他**强制刷新一次**（Ctrl+F5）或清站点数据——这只是浏览器启发式缓存，不是 SW。

**失败怎么办**：
* 白屏/JS 报错 → `bash /tmp/deploy/rollback.sh --web-host prod --yes`（30 秒换回老 dist）；
* `index.html` sha 不一致 → 解包/覆盖路径错了（`rsync` 源目录别多一层）；
* 403 → 属主不是 `www-data`（`chown -R www-data:www-data`）。

---

### T+45（02:45）真客户端抽检

> 依赖 `compat-harness` / 前端同事的产物；没有产物就跳过并明确记录"未验"。

| 客户端 | 动作 | 成功判据 |
|---|---|---|
| **Windows EXE** | 跑 `EndfieldBakerChat-compat.exe`（或旧版 EXE 指向 `https://api.peilika.beer`），发一句"在吗" | 2–5 秒内出字；不是"网络错误"；`/instances` 里能看见这台设备 |
| **安卓 APK** | 装 `BakerAI-3.2.4-rc.apk`（或线上 3.2.2），发一句 | 同上；**装的时候注意签名**（必须是 `61:ED:37:7E…:AF:81`，否则会要求卸载） |
| **网页版** | `https://chat.peilika.beer`，登录/访客进聊天，发一句；再试"群聊"建 2 人群 | 出字；群聊能建/发言；刷新后记录还在 |
| **老客户端云同步**（可选） | 导出数据包 → 上传 → 记取件码 → 再取回 | 码 8 位；取回字节一致（**大包 60 MB 见 T+55**） |

**失败怎么办**：
* EXE/APK 报 404/405 → 老形状路由没生效（`nginx -T | grep -c bakerai-new-api`，再看 `/chat` 单条 curl）；
* APK 要求卸载 → 签名不对（**不要在窗口里换签名**，停下来）；
* 网页登录失败 → `/v1/auth/*` 没反代（`--web` 漏了该 vhost）。

### T+55（02:55）大包上传实测（风险 3 的验证）

```bash
# 造一个 ~60MB 的假导出包（JSON 填充）
python3 - <<'PY'
import json
pad = "x" * (60*1024*1024)
json.dump({"probe":"big","pad":pad}, open("/tmp/big.json","w"))
PY
ls -lh /tmp/big.json
time curl -sS -o /tmp/big.resp -w 'http=%{http_code} time=%{time_total}s\n' \
  -H 'Host: api.peilika.beer' -H 'Content-Type: application/json' \
  --data-binary @/tmp/big.json http://127.0.0.1/upload
cat /tmp/big.resp
```
**成功判据**：`http=200`，响应里有 8 位 `code`；耗时 < 100 s（CF 限制）。
**失败怎么办**：
* 413 → `client_max_body_size` 没到 64m；
* 504 且耗时 ≈ 120 s → 命中了某个 vhost 的 120 s 超时（确认 `/upload` 用的是 300 s 那个 location）；
* 走公网（`https://api.peilika.beer/upload`）在 ~100 s 被断 → **CF 限制**，记录为已知缺陷，
  规避方案：老客户端云同步继续走 `local.peilika.beer`（本次不切它）。

---

### 3.5 web 上线（先 A 预演 test2 → 再 B 正式覆盖 chat）

**前提**：后端已切（§1.9）、`backup_web.sh` 跑过。**B 没在 A 全绿 + 审计前绝对不动。**

```bash
# 0) 备份两块 dist（目录副本 + tar + INDEX.txt 里的 sha256/文件数/入口 js）
bash /tmp/deploy-new/deploy/backup_web.sh
#   产物：/opt/bakerai-backup/<ts>_web/{web-v2,web-chat}[/,.tar.gz] + INDEX.txt

# 1) A（test2）—— 先只换 dist，不切 vhost 也能验静态页
bash /tmp/deploy-new/deploy/deploy_web.sh --host test2 --dist /tmp/newdist
#   它做：备份 → rsync -a --delete（排除 .well-known/）→ sha256 核对
#         → 带 Host 头打首页（要求 200 且含新入口 js 文件名）→ 失败自动回滚

# 2) A 的四步验收（缺一不许上 B）
#   ① 首页 200 且含新入口：curl -H 'Host: test2.peilika.beer' 127.0.0.1/ | grep <新 index-*.js>
#   ② 同源 /v1/meta 200：  curl -H 'Host: test2.peilika.beer' 127.0.0.1/v1/meta
#   ③ 同源预检 200：        curl -X OPTIONS -H 'Host: test2...' -H 'Origin: https://test2.peilika.beer' #                              -H 'Access-Control-Request-Method: POST' 127.0.0.1/v1/auth/device
#   ④ 迁移 banner：用一份造的老 IndexedDB/localStorage 打开页面，应出现"迁移提示条"
#      （做法：无头 Edge + CDP，先写入老库再加载页面；脚本见 _tmp/compat 里的 cdp 工具）
#   ⚠️ ②③ 需要该 vhost 已插 include：先 switch_nginx.sh --web staging --api no，
#      验完再决定是否回滚 vhost（今晚 B 也要 include，所以 A 验完通常保留）

# 3) B（chat 正式）—— 只在 A 四步全绿后
bash /tmp/deploy-new/deploy/deploy_web.sh --host prod --dist /tmp/newdist

# 4) 回滚 web（换回备份 dist + reload；不碰库、不碰后端）
bash /tmp/deploy-new/deploy/rollback.sh --yes --web-host prod    # 或 test2 / both
```

**vhost 模板**（`deploy/nginx/`，不自动 apply）：
| 文件 | 用途 |
|---|---|
| `web-test2.conf.template` | A 站整份配置（root/SPA/index 不缓存/两条 include/64m） |
| `web-chat.conf.template` | B 站同上 + 保留原有 `/api/legacy-key` 反代 |
| `bakerai-new-api.conf` | 公共反代（**不含 `/group/`**，否则 api 系 vhost duplicate） |
| `bakerai-web-extra.conf` | 网页域专用（`/group/`），只给网页 vhost 插 |

## 4. 回滚（目标 3 分钟内）

```bash
# 0) 先看要做什么（不动任何东西）
bash /tmp/deploy/rollback.sh --dry-run

# 1) 最常见：入口回老 gateway（网页 dist 不动）
bash /tmp/deploy/rollback.sh --yes --nginx-only

# 2) 网页也回滚（覆盖错了/白屏）
bash /tmp/deploy/rollback.sh --yes --web-host prod
#    预演站：--web-host staging；两个都回：--web-host both

# 3) 彻底放弃这次上线（连新后端一起停；库与文件保留）
bash /tmp/deploy/rollback.sh --yes --stop-new
```

**它做什么**：
1. 用 `/opt/bakerai-backup/state/nginx-last.txt` 里登记的备份，把改过的 vhost **原样还原** → `nginx -t` → `reload`；
2. 确认 `perlica-*` 还在跑（切换时没杀，所以通常不用重启；< 20 个才尝试重启 gateway/group）；
3. 按需把 web dist 从 `web-prod-dist.tar.gz` / `web-stage-dist.tar.gz` 还原（`rsync -a --delete` + chown）；
4. 验证：`api.peilika.beer/health` 必须回到 `gateway_service`，`/chat` 必须 200/400/422（不是 404/502）。

**数据库为什么不动**：
* 老后端的状态**不在 MySQL**（进程内存会话 + `run/group_sessions.db` + JSON 配置），它根本不读 `baker_back`；
* 迁移 001–019 全是加表/加列/加索引，没有破坏性改动；
* 留着库 = 保住这段时间新产生的消息，重新上线时不用重灌。
* 只有"数据被写坏"才整库回滚（会丢数据）：
  `gunzip -c /opt/bakerai-backup/<ts>_predeploy/mysql-baker_back.sql.gz | mysql -uroot baker_back`

**回滚后要不要再切回来**：修好之后直接重跑 `switch_nginx.sh --web both --api yes`（幂等，会重新写 include 与 upstream）。

---

## 5. 脚本清单

| 脚本 | 跑的人 | 作用 | dry-run |
|---|---|---|---|
| `backup.sh` | root | 备份老代码/新代码/nginx/api_pool/SQLite/两块 dist/MySQL/配置 → `/opt/bakerai-backup/<ts>_<tag>/` + sha256 | ✅ |
| `install_mysql.sh` | root | **只在没装时**装 8.4 + 建库建号（当前机器会自检后直接退出） | ✅ |
| `deploy_backend.sh` | `y` | 两阶段：`--stage`（rsync→依赖→迁移→47 key/147 世界书/v2，**不启服务**）／`--start`（只装 unit + 起服务 + 健康断言）；无参数=两段连做 | ✅ |
| `backup_web.sh` | root | 只备份两块 web dist（目录+tar+sha256/文件数/入口 js）→ `/opt/bakerai-backup/<ts>_web/` | ✅ |
| `deploy_web.sh` | root | 换一份 dist：备份→`rsync -a --delete --exclude=.well-known/`→sha256→Host 头验收→失败自动回滚 | ✅ |
| `switch_nginx.sh` | root | 装 include 片段 + 换 upstream 端口 + 插同源 include → `nginx -t` → reload → Host 头逐条验证；失败自动还原 | ✅（只打 diff） |
| `smoke.sh` | `y` | 11 项冒烟（health/readyz/instances/meta/device/characters/chat/group/upload→fetch/web） | —（加 `--generate` 才真出话） |
| `rollback.sh` | root | 还原 vhost / 还原 dist / 可选停新后端；DB 保留并说明原因 | ✅ |
| `systemd/baker-api.service` | — | systemd **用户**单元（`Restart=always`、`EnvironmentFile`、`WorkingDirectory`、venv uvicorn、`127.0.0.1:5660`） | — |
| `nginx/bakerai-new-api.conf` | — | 同源反代 include 片段（`/v1` `/chat` `/group/` `/instances` `/upload` `/fetch` `/readyz`；SSE 关缓冲；上传 300 s / 64m） | — |
| `nginx/bakerai-web-extra.conf` | — | 只给**网页 vhost** 的补充片段（`/group/`；api 系自带，混用会 duplicate） | — |
| `seed/worldbook_endfield.sql` | — | 147 条世界书（REPLACE INTO，幂等） | — |
| `lib/common.sh` | — | 日志/root/HTTP/JSON 小工具 | — |

所有脚本：`set -euo pipefail`、幂等、失败退出非 0、不打印任何密钥/密码（只打印文件路径）。

---

## 6. 已知缺口与后续（不阻塞 02:00）

1. **世界书源文件不在 `BakerAI_new` 里**：权威源是
   `character_cardsrknights-endfield-cards-neo\世界书\终末地世界书_通用版_v1.0.md`（141 KB），
   已复制进 `deploy/seed/` 随包发布；`worldbook_endfield.sql` 保留为无 md 时的退路。
2. **`local.peilika.beer` 的超时**：`proxy_read_timeout 120s` < 老客户端 300 s。本次不切它；要切的那天一并改 300 s。
3. **Cloudflare ~100 s 响应限制**：大包上传可能撞上；已列为风险 3，T+55 实测。
4. **`/api/legacy-key` 不在新 vhost 里**：按决定"先不配"。新后端不需要它（key 在服务端池里）。
5. **`baker_back_test` 库没灌种子**：新后端测试环境要用时再跑一遍 `deploy_backend.sh --db-url ...`。
6. **机器待重启**（`/var/run/reboot-required`）：上线稳定后再约窗口，不要在 02:00 动。
7. **`baker-v4.service`**：T-0 停掉后会一直是 disabled；确认不再需要后可删单元文件（`rollback.sh` 不依赖它）。

---

## 6.5 切换当天踩到并修掉的 4 个坑（下次换机必看）

### ① `duplicate location "/group/"` —— 共享片段不能放 api vhost 已有的 location
* 现象：`switch_nginx.sh` 第一次执行，`nginx -t` 报
  `duplicate location "/group/" in /etc/nginx/sites-enabled/api-peilika:70`。
* 原因：`api-peilika` / `api2-peilika` **自己就有** `location /group/`；
  我又在共享片段 `bakerai-new-api.conf` 里放了一份，同一个 server 块里重复。
* 修法（已落地）：
  * 共享片段 `bakerai-new-api.conf` **只放 api vhost 没有的** location（`/v1/` `/v1` `/upload` `/fetch` `/readyz` `/health` 等）；
  * 网页域需要的 `/group/` 单独放 `bakerai-web-extra.conf`，`switch_nginx.sh --web` 时给网页 vhost **插两条 include**；
  * 已在 `/tmp/nginx-test2` 副本上 `--web both --api yes` 验过：`nginx -t` 成功、无 duplicate。
* 教训：**改 nginx 前先在 `/tmp` 副本上 `--nginx-root` 跑一遍**（`nginx -t -c 副本 -p 副本`），别直接上生产试。

### ② `rollback.sh` 找不到 `/opt` 备份就直接 `die` —— 等于回滚不可用
* 现象：切换失败触发自动回滚，`rollback.sh` 报"在 /opt/bakerai-backup 下找不到备份"直接退出，
  **一行 nginx 都没还原**（当时靠 switch_nginx 自己的还原兜住了）。
* 原因：脚本把"网页 dist 还原"的前提（必须有 backup.sh 的产物）当成了所有回滚的前提。
* 修法（已落地）：没有 `/opt` 备份时**降级为只还原 nginx**（state 文件里登记的 `.bak_bakerai_*` 足够），
  网页 dist 那步跳过并明确警告。已 `--dry-run` 验证：只打印 `cp` 两条 + `nginx -t && reload`。

### ③ root 下 `systemctl --user` 数 perlica 单元永远是 0
* 现象：`rollback.sh` 在 `-Sudo` 下报"perlica-* 正在运行的单元数：0"，误判"服务没起来"。
* 原因：root 连不到 y 的 user bus。
* 修法（已落地）：root 时改用 `sudo -u y env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user …`；
  现在正确显示 **29**。

### ④ reload 之后立刻检查会拿到旧结果
* 现象：切换后脚本内第一条 `/v1/meta` 检查返回 **404**（看起来像切换失败），1 秒后手工再查就是 200。
* 原因：`systemctl reload nginx` 是**优雅重载**，老 worker 还会活一小会儿；检查跑得太早。
* 修法（已落地）：`check()` 改成**最多重试 5 次、每次间隔 1 秒**再判失败。

> 另外一条环境坑（首次 `--start` 就踩到）：**用户单元里不能开 `PrivateDevices=true`** ——
> systemd 会报 `Failed to drop capabilities: Operation not permitted` → `status=218/CAPABILITIES`
> → 起不来且 `Restart=always` 无限重试。模板已去掉该行（`NoNewPrivileges` 一并去掉）。

## 7. 本地/预演演练结果（2026-11-18 00:45–01:05）

| 演练 | 做法 | 结果 |
|---|---|---|
| **语法** | `bash -n` 过 7 个脚本（WSL + 服务器各一遍） | ✅ 全过 |
| **nginx diff** | `switch_nginx.sh --dry-run`，用**本机伪造的配置副本** + `--nginx-root` 跑 | ✅ diff 只动该动的：api/api2 各 **2 行 upstream + 3 行插入**；chat/test2 各 **3 行插入**，其它一行没变 |
| **nginx 真校验** | 把 `/etc/nginx` 复制到 `/tmp/nginx-test`，应用改动后 `nginx -t -c 副本 -p 副本` | ✅ `syntax is ok / test is successful`（include 与 `location` 无重复冲突） |
| **nginx 真配置 dry-run** | 对服务器 `/etc/nginx` 跑 `--dry-run`（只读） | ✅ 同样的最小 diff；`nginx -t` 基线通过；生产配置**未被修改**（无 `.bak_bakerai_*`，nginx 仍 active） |
| **backup** | 服务器上 `backup.sh --dry-run --dest /tmp/bk --no-db` | ✅ 10 个步骤全部打印出正确命令与路径 |
| **deploy_backend** | 服务器上 `--src /tmp/stage --skip-seed --dry-run` | ✅ 预检读到 `Python 3.14.4` / `baker_back`（迁移 002_seed）/ 5660 被 `baker-v4` 占用；命令打印**已打码**（`baker:***@`） |
| **smoke（本地 5660）** | `smoke.sh --base http://127.0.0.1:5660` | ✅ **12 PASS / 0 FAIL / 1 SKIP**（含 `/upload`→`/fetch` 逐字节往返、群聊建/关、`/chat` 400 形状） |
| **smoke（服务器影子 5660，只读 GET）** | 生产影子服务 | ⚠️ 预期会失败：`/instances` **404**、`/readyz` `migrations=002_seed`、`/v1/meta` caps 只有 4 项 → **现在切过去，老客户端会坏**（这正是要先 deploy 的原因） |
| **种子链路** | 本地 WSL 新建 `baker_deploy` 库（不碰 baker_back/up/v2） | ✅ 迁移 **19 个（001…019）**；假 key 3 条；v2 长卡 **36**；世界书 md **147**；`REPLACE` 重跑仍 147（幂等） |
| **rollback --dry-run** | 伪造备份目录 + 状态文件 | ✅ 能按状态文件还原对应 vhost、能识别缺 dist 并跳过 |

**演练中修掉的 6 个问题**（都是真跑才暴露的）：
1. `smoke.sh` 里 `my_role` 传中文 → 后端要求 `admin/observer/成员名`（已改 `admin`）；
2. `smoke.sh` 一处引号笔误导致整脚本语法错（`bash -n` 抓到）；
3. `switch_nginx.sh` 的 sed 用 `#` 当分隔符，而插入内容含 `#` 注释 → 报 `unknown option to 's'`（改成 `|`）；
4. upstream 替换多打了一个空格（`server  127.0.0.1:5660`）→ 修正为单空格；
5. `deploy_backend.sh` 在 dry-run 里**把带密码的 DATABASE_URL 原样打印**（已改 `run_masked` + `mask_url`，现在只显示 `baker:***@`）；
6. `rollback.sh` 解析状态文件时把 `ts=`/`web=` 也当备份路径 → 刷一堆"备份文件不在了"（已按 `backup=` 前缀过滤）。

---

## 8. 02:00 现场检查单（打印出来照着勾）

```
[ ] T-24h 备份跑过，/opt/bakerai-backup/<ts>_predeploy/MANIFEST.sha256 有内容
[ ] cryptography / python-multipart 已装（deploy_backend 步骤 3 的 ✓ 出现过）
[ ] 迁移 = 019_prompt_variant
[ ] keys total=47 enabled=25
[ ] worldbook=147、characters=36、v2=36
[ ] switch_nginx --dry-run 的 diff 只有 4 个文件、每块 ≤3 行
[ ] baker-v4 已停、5660 已释放
[ ] baker-api active，/health /readyz /instances /v1/meta 全对
[ ] smoke --generate = FAIL 0（必须真出过一条话）
[ ] 网页 test2 先上、再 chat；index.html sha256 与构建产物一致
[ ] EXE / APK / 网页各发一句
[ ] 回滚命令在手上：bash /tmp/deploy/rollback.sh --yes --nginx-only
```

---

## 附录 A：本次实测到的"坑清单"（照做就不会踩）

| 坑 | 事实 | 规避 |
|---|---|---|
| 5660 已被占 | `baker-v4.service` 正在跑（11-15 的影子，代码旧、库只到 002） | T-0 先 `disable --now`，再起 baker-api |
| 影子库是"半成品" | `schema_versions` 只有 `001_init, 002_seed`；`api_keys=0`、`characters=3`、无 `worldbook` 表 | 必须完整跑迁移 + 种子（不要以为"库已经建好了"） |
| 依赖缺 2 个 | venv 里没有 `cryptography` / `python-multipart` | `deploy_backend.sh` 步骤 3 会装并断言 |
| 服务器没装 `sqlite3` | `command -v sqlite3` 为空 | 备份 SQLite 用 `python3` 的 `sqlite3.backup()`（脚本已这么做） |
| nginx 有 600 的 vhost | `peilika-v2` / `peilika-mobile` / `downloads` 等只有 root 能读 | 读配置/切 nginx 一律 root |
| 全局 20m | `nginx.conf` 里 `client_max_body_size 20m` | 新 vhost 里覆盖成 64m（模板已带） |
| 无 ufw | `ufw` 不活动，iptables 只有 Docker 链 | **绝不要**把 5660/3306 绑 0.0.0.0 |
| 老日志 3.8 G | `baker/backend/logs` 无轮转 | 不在本次范围；备份已排除 logs |
| 群聊 WAL 4 MB | `group_sessions.db-wal` 比主库还大 | 备份用在线 backup API（已做） |

## 附录 B：变更记录（上线后追加到 skill `references/07`）

```markdown
### 2026-11-18 02:00 新后端 + 新 web 上线
- 备份：/opt/bakerai-backup/<ts>_predeploy（sha256 见 MANIFEST）
- 后端：/home/y/baker-v4/back（代码 <commit/时间>），迁移 019，keys 47/25，worldbook 147，v2 36
- 单元：baker-api.service（用户单元，127.0.0.1:5660，Restart=always）；baker-v4 disable
- nginx：api/api2 upstream→5660；chat/test2 插 include（同源 /v1 …）
- web：/var/www/peilika-v2（先）→ /var/www/peilika-chat（后），dist sha256 <...>
- 验证：smoke 13/13 PASS（含 --generate 真出话）；真客户端：EXE ✅ / APK ✅/未验
- 回滚：rollback.sh --yes（实测/未实测）；实际未触发
- 遗留：local.peilika.beer 超时 120s；CF 100s；机器待重启
```

---
