# 变更记录（CHANGES）

> 规则：**每次改动都在最上面加一节**，写清楚「改了什么 / 为什么 / 怎么验 / 怎么退」。
> 这份文件是给不看代码的人看的 —— 每一节都应该能在三分钟内读完。
> 代码本身在同一个 git 仓库里，提交信息与这里的日期一一对应。

---

## 2026-11-22 16:25 · 语料补到 4871 条（PRTS 角色档案 + 手写补充 + 2.0 前瞻）+ 老 RAG 的 `/retrieve` 改由算法回答

> 站长口径：「1.更新语料库，基本条目保持不变，新增条目按照本地prts语料库去写 2.补充 3.所有rag请求转到算法搜索，在本地验证」。

### 改了什么

**一、语料库：base 不动，加三份补充（一起导入、一起被 `--prune` 保护）**

| 文件 | 条数 | 来源 |
|---|---|---|
| `supplement_prts.jsonl` | 422（33 人） | **本地 PRTS 语料库**（`$DSH_HOME/prts-corpus/releases` 的 `character_profile`）—— 官方原文，按句切成 ~400 字一条，只去掉每行的小节前缀 |
| `supplement_handwritten.jsonl` | 23 | 手写世界书里 RAG 语料**一次都没出现**的那 23 条（规则/机制为主：职业与元素、协议传送规矩、据点建设规矩…） |
| `supplement_preview.jsonl` | 18 | 2.0 前瞻公开内容（明晦炉 / 阵列·明·晦 / 重建禁区 / 白垩洲 / 罗德岛陆行舰 / 小刻 / 米米诺…）。**PRTS 那份是 11-02 的，还没有 2.0**，这批取自 11-21 前瞻直播整理，`source: "preview"` |

- 生成脚本 `scripts/build_supplement_prts.py`（读 PRTS 库，`--check` 只统计）；
  导入脚本新增 `--no-supplement`（只导 base，排查用）。
- 最终：**4871 条 / 587 个标题**（base 4408 + 补充 463），索引 **366 行**（人物 33 行 + 设定 + 规则）。

**二、老 RAG 的检索口子改由算法回答（`app/api/rag_compat.py`）**

- 新增 `POST /retrieve`：body 还是 `{query, top_k, min_score}`，**一次返回两套字段**——
  `results`（网关形状：rank/score/title/category/text）给网页/APK，
  `passages`（id/doc_id/category/title/text/score）给老后端 `rag_client.py`。
- 新增 `GET /stats`（语料规模/模式）；`GET /health` 补 `ok: true`（`rag_client.healthy()` 读的就是这个字段）。
- 分数：算法分（几十的量级）映射到 **0.40~0.99**（本批最高 0.99），老客户端那个 `min_score=0.4` 的阈值照样说得通。
- CORS 由全局中间件给（`*`），WebView 的 `https://localhost` origin 能直接打。

**三、检索侧两处配套（都是为了"一篇几十块"的档案）**

- `prefetch` 新增 `max_per_title=2`，并把候选窗口开到全量：
  不这么做时，角色档案一篇几十块会把 12 个候选和前几十个排序位全占满 ——
  实测同一轮问「陈千语档案 + 明晦炉 + 据点建设规矩」，后两个根本进不来。
- `/retrieve` 同一篇最多给 2 块（老 RAG 也是一块一块返回的，但不该一篇霸屏）。

### 为什么

- 语料停在 11-08 → 2.0 新词、角色档案都缺；PRTS 本地库有官方角色档案，正好按站长说的"按 PRTS 写"。
- 那张 GPU 上的向量服务每天还有 300+ 次请求（8 成来自一台 Android WebView 的老版本）。
  口子接到算法检索以后它就可以关，老客户端不掉功能。

### 怎么验（本地 WSL 栈）

- `/retrieve`（不带 token，老客户端就是这么打的）：
  「明晦炉是什么」→ `明晦炉/archive/0.99`；「陈千语的档案」→ `character` 两条档案；
  「四号谷地当年发生了什么」→ 两条 archive；「职业与元素」→ 手写补充。
  `elapsed_ms` 2~150ms；`/stats` → `entries=4871, titles=587`；OPTIONS 预检带 `access-control-allow-origin: *`。
- 聊天预取（三条真请求）：第 1 条命中 `（陈千语）`、第 2 条加 `（明晦炉）`、第 3 条加 `（据点建设规矩）`；
  索引行 `共 366 条`；system 7.4k→9.2k token（多话题同轮时）。
- `pytest tests/test_rag_compat.py tests/test_rag_corpus.py tests/test_retrieval.py tests/test_prompt.py tests/test_generate.py tests/test_legacy.py`。
- 语料体检 `scripts/eval_rag_corpus.py`：base 部分数字与上一版**完全一致**（标题问法 top1 94.0% / 前 12 100%，点名型 97.3%）。

### 怎么退

- 关掉补充：`scripts/import_rag_corpus.py --no-supplement --prune` 重导一次（base 不变，补充条目被清掉）。
- 撤掉检索口子：`git revert` 本次提交（`app/api/rag_compat.py`、`main.py`、`api/__init__.py`、`tests/test_rag_compat.py` 一起走）；
  nginx 不动的话 `rag.peilika.beer` 仍旧指向老网关，本来就没改。

### 已上线（2026-11-22 17:24，正式站）

- **备份先做**：`app.bak_20261122_172310`、`scripts.bak_20261122_172310`、
  `backups/worldbook_20261122_172310.sql`（147 行，123 KB）、`backups/rag-peilika.20261122_172402`。
- **语料**：部署前 `worldbook` 147 行（手写那版）→ 导入 `base 4408 + 补充 463`，
  「新建 4871 / 删除 147」，库里现在是 **4871 行 / 587 个标题**。
- **服务**：`baker-api` 重启 `readyz 200`；生产机上 `pytest tests/test_rag_corpus.py tests/test_rag_compat.py` 全绿（py3.14）。
- **nginx**：`rag.peilika.beer` 的 `proxy_pass` 5950 → **5660**，`nginx -t` 通过后 reload。
  外网实测 `POST https://rag.peilika.beer/retrieve` 返回 `model: "worldbook-algo"`，
  `Origin: https://localhost` 也带 `access-control-allow-origin: *`。
- **老网关没停**：5950 / 5921 / 5922 照旧跑着（回滚用），观察一天再停。

回滚三步：
1. nginx：`sudo cp ~/baker-v4/backups/rag-peilika.20261122_172402 /etc/nginx/sites-available/rag-peilika && sudo systemctl reload nginx`
2. 语料：`MYSQL_PWD=… mysql … baker_back < ~/baker-v4/backups/worldbook_20261122_172310.sql`
3. 代码：`cp -a ~/baker-v4/back/app.bak_20261122_172310 ~/baker-v4/back/app && systemctl --user restart baker-api`

---
## 2026-11-22 15:55 · 世界书语料换成老后端的 RAG 库（不再手写 147 条）

> 站长口径：「算法查找的库改一改，不要自己写的，直接用老后端的 rag 数据库，改写为可查找条目作为语料库使用。在本地跑起来验证，先不推生产」。

### 改了什么

- **新增 `app/retrieval/corpus.py`**：把老 RAG 语料一块一块改写成世界书条目 ——
  标题取文件名（`刀剑无名·其一`），正文是语料原文（只剥掉开头那行【分节标题】，一个字不改），
  触发词从语料自身统计（文件标题 / 分节标题 / 剧情里的说话人）。
  **太常见的词（出现在 2% 以上块里）和单字词不当触发词** —— 一个「方」能让半本语料都命中。
- **新增 `scripts/import_rag_corpus.py`**：读 `data/rag_corpus/docs.jsonl` 写 `worldbook` 表。
  幂等（按 `source_id` 覆盖），`--prune` 顺带删掉旧的手写条目，`--dry-run` 只统计。
- **新增 `data/rag_corpus/`**：老库 `index-endfield-v2/docs.jsonl` 的**原样拷贝**（3.2 MB，
  4408 块 / 547 个文件 / 83.7 万字）+ README（来源、字段、换语料的正确姿势）。
- **`retrieval/store.py` 三处**：
  - `match_entries` 加了「标题整串出现在提问里」的加分（`TITLE_BONUS + 标题长度`）。
    没有它时问「超域试验场在哪」会被「超域」抢走 —— 两个标题都在提问里，只能比标题排序。
    三档的 exact 档本来就有 +30 的标题加分，这次是把它补到预取这条路上。
  - `build_index_text`：同一标题的多个块只列一行，`event`（剧情）条目不列目录
    → 目录 301 行、约 2.2k token（不去重是 4408 行，不跳剧情是 545 行）。
  - `prefetch`：同一标题的几块并成一段，模型不会再看到一串重复的（四号谷地）（四号谷地）。
- `settings.worldbook_index_enabled` 的注释、`docs/01-工程进度总览.md` 的进度表 /
  提示词结构图 / 数据现状三处同步。

### 为什么

- 那 147 条是**手写的**：触发词是人想的，正文是摘写的。语料一更新就跟不上，两份真源迟早打架。
- 老 RAG 库是纯数据（向量索引之外就是 `docs.jsonl`），把向量检索关掉之后它就是唯一真源。
- **算法没换**：还是 `app/retrieval/` 那套词法检索，换的只是"库"。

### 怎么验（本地 WSL 栈，未碰生产）

- `scripts/eval_rag_corpus.py`（新增，换语料前后都该跑）：
  - 标题问法 1446 条（每条设定条目的三种问法）→ **top1 94.0% / 前 12 命中 100%**；
  - 老库自带的 10099 条合成问法 → 点名型 2291 条 **前 12 97.3% / top1 88.0%**，
    摘录型 7808 条前 12 只有 19.6%（那是把正文首句直接当问题，真实对话不会这么问）。
- 真栈 4 条请求（`_audit/wsl_rag_corpus_check.sh`）：system 里目录 `共 301 条`、
  预取命中 `（四号谷地）`/`（超域试验场）`/`（刀剑无名·其一）`、**超域试验场排在超域之前**、
  闲聊那句不预取；单轮 76–252ms（首轮含启动）。
- `pytest tests/test_rag_corpus.py tests/test_retrieval.py` → 26 passed；回归子集另跑。

### 已知缺口

- 语料停在 **2026-11-08**，2.0 前瞻的新词（明晦炉、阵列·明/晦、起源锻造液）里面没有 → 问这些检索不到。
  要补只能拿新语料重建索引（老库的 `build_index.py` + 那块 GPU），不是改代码能解决的。
- 手写库里那 23 条语料里没有的"规则/机制"词条（职业与元素、协议传送规矩、据点建设规矩…）
  跟着一起没了 —— 站长确认要不要留一份小补充库。

### 怎么退

1. 数据库：把 `deploy/seed/worldbook_endfield.sql`（147 条那份）导回去，重启后端；
2. 代码：`git revert` 本次提交（改的文件：`app/retrieval/corpus.py`、`app/retrieval/store.py`、
   `app/settings.py`、`scripts/import_rag_corpus.py`、`scripts/eval_rag_corpus.py`、`tests/test_rag_corpus.py`、
   `data/rag_corpus/`、`docs/01-工程进度总览.md`）。

---

## 2026-11-22 02:45 · 全站采样参数改成 temp=1.3 / top_p=0.7

> 站长口径：`temp=1.3，topP=0.7` → 「全部模型输入按照这个设置」。

### 改了什么

- `settings.upstream_temperature` 默认 **0.8 → 1.3**；新增 `upstream_top_p = 0.7`。
- `upstream._build_payload` 每次请求都带上 `top_p`（取 settings；调用方也能按次覆盖）。
  **配 0 / null = 不发这个字段**（用上游默认），不是"top_p=0"——那会让模型只能从最大概率那个词里挑。
- 两栈 `.env` 同步改：`BAKER_UPSTREAM_TEMPERATURE=1.3` + 新增 `BAKER_UPSTREAM_TOP_P=0.7`。
  ⚠️ 这两行原来在 `.env` 里钉着 0.8，**只改代码里的默认值是不生效的**（配置优先），
  所以必须两边一起改——已经踩过一次。
- **不变的两处**：摘要/压缩（`memory.py` 显式 0.2）和群聊主持判定（`group.py` 显式 0.3），
  它们是"要稳"的内部调用，仍然用自己的温度；只是也带上了全站 top_p。

### 怎么验

- 新增 `tests/test_sampling.py` 6 条：默认 1.3/0.7、流式同样、调用方覆盖优先、
  单独改 top_p、`top_p=0` 等于不发、settings 改了立刻生效 → 与重新生成/兼容两套合计 **20 passed**。
- 本地假上游抓请求体（本地栈 `_audit/wsl_mock_log.py` 现在把 temperature/top_p 也记下来）：
  普通发送、老客户端重发生成的、显式 regenerate 三种调用实测都是 `temp=1.3 / top_p=0.7 / max_tokens=2048`。
- 生产机：`pytest tests/test_sampling.py + test_client_echo.py + test_regenerate.py` 退出码 0；
  `get_settings()` 读出 `1.3 0.7`；真上游冒烟三次回话**互不相同**（改之前三次里两次开头一模一样）→ PASS。

### 怎么退

`.env` 里把 `BAKER_UPSTREAM_TEMPERATURE` 改回 0.8、删掉 `BAKER_UPSTREAM_TOP_P` 一行，
重启 `baker-api` 即可；代码默认值想一起回退就改 `settings.py` 两个数。

---

## 2026-11-22 02:05 · 「重新生成几乎没有效果」：老客户端那句重发，服务端认成重新生成

> 群友原话：「现在的重新生成几乎没有效果……如果有一个对话聊炸了，就是突然提了剧情完全无关的东西，
> 我重新生成，仍然是接着他刚才的话」「重新生成了几次后，他会说为什么会说好几遍」
> 「以前刷新还有概率让他接上，现在根本接不上了」。

### 病根：客户端那一版还在"把那句话再发一遍"

服务端昨天（11-21 15:50）已经把「重新生成」做成一个显式动作（`regenerate: true`），
网页也改好了；但**已经装在用户手机/电脑上的客户端**（4.0 之前）没跟着更新，
它点重新生成走的是老路：把玩家那句话当成一条新消息再 POST 一次 —— 服务端看到的和
"玩家又说了一遍"一模一样。于是：

1. 历史里多出一条一模一样的玩家消息 → 她看到"你怎么把同一句说了好几遍"；
2. 那句"聊炸了"的回话还留在历史里，新回复当然还是接着它编 → 「重新生成没效果」。

生产库实证（`baker_back.messages`，11-21 16:15 部署之后仍在发生）：
`conv 1401`（iPhone 网页）02:05:30 有一条种子导入写进的玩家消息，1.3 秒后又来一条一模一样的
（`i337-30` / `c-mwqpxrqy-xjib28`）；`conv 1394`（安卓客户端）同一句隔 7~17 秒重复了 4 次 ——
全是"重新生成"点出来的。

顺带查明了第二个来源：客户端的**种子导入**里混进了"正要发出去的这一句"（`prepareTurn` 的
history 与本地消息是同一个数组），导入写成一行、紧接着又发一行，也是两条一样的玩家话。

### 改了什么（只改服务端，用户不用更新客户端）

`api/conversations.py` 新增 `_plan_client_echo()`，在正常发消息之前先判断一次：

> **正文与最后一条玩家消息一字不差、而且那条是刚刚发的（默认 5 分钟内）
> → 按「重新生成」处理**：清掉这条之后的旧回复、`next_seq` 退回原位、
> 新回复落回原来的 seq，**不新增玩家消息**。

刻意留的边界：

- 群聊不管（群聊那一轮的历史是客户端带上来的，走另一套）；
- 带新附件的不算（连发两张图正文都是「[图片]」，按正文匹配会把第二张吞掉）；
- 同一条 `client_msg_id` 的重放先放过 —— 那是幂等重试，仍回 `duplicate: true`；
- 锚点后面只允许是"已经生成好的回话"，出现别的玩家消息就不猜；
- 时间窗 `BAKER_REGENERATE_ECHO_SECONDS`（默认 300，设 0 关掉这条兼容）。

新客户端不受影响：它直接带 `regenerate: true`，走的是昨天的显式分支，压根到不了这里。

### 怎么验

- 本地 WSL（MySQL 8.0 / py3.12）：新增 `tests/test_client_echo.py` **7 passed**
  （重发一次/连点两次/种子导入重发/时间窗过期/开关关掉/不同的话/群聊不适用），
  `tests/test_regenerate.py` 7 passed；
  回归 `idempotency/continue/attachments/legacy/smoke/imports/group/generate/prompt/ratelimit`
  合计 **108 passed / 0 failed**。
- 生产机（MySQL 8.4 / py3.14）：`tests/test_client_echo.py + test_regenerate.py` **14 passed**。
- 生产真上游冒烟（`_audit/tmp/prod_echo_smoke.py`，跑完即删会话）：
  同一句连发三次 → `rows: [(1,'user'),(2,'assistant')]`、三次 `reply_seq` 都是 2、三份回话互不相同 → **PASS**。

### 怎么退

后端：`cp -a app/api/conversations.py.bak_20261122_022815 app/api/conversations.py`（settings 同目录同后缀）
＋ `systemctl --user restart baker-api`。前端没动。

### 留下的账

- **老会话里的重复行不会自己消失**：修复只保证"以后不再加"。不过现在点一次重新生成，
  会把这一轮之后的全部旧回复和重复玩家话一起清掉，等于顺手把那一段治好了。
- 全库历史上还有 789 对相邻重复，需要的话可以另写脚本（先导备份再删）—— 那是动真实用户数据，等站长点头。

---

## 2026-11-21 16:20 · 群聊「@」按钮换成站长手绘的图标



> 站长原话：「群聊 at 按钮直接用我画的吧」（附一张 118×118 的图：黑 @ + 一圈细描边）。

### 改了什么

- 新增素材 `front/src/assets/materials/icon_sns_chat_at.png` —— **原图直接入库**，没重画、没描边、没改粗细。
  和客户端 `BakerAI_new/client/src/assets/materials/icon_mention.png` 是**同一张图**（sha256 一致），两个平台从此长得一样。
- `constants/materials.ts` 素材表加 `editBtnAt`。
- `components/chat/ChatInput.vue`：@ 按钮里原来是拿字体排的 `<span class="chat-input__at">@</span>`（43px / 字重 900），
  现在换成 `<img class="chat-input__face">`，**铺满整颗圆按钮**（四周留 1px：`top/left:1px; width/height:calc(100% - 2px)`，
  `object-fit:contain`、`border-radius:50%`）。原来那段调字号、调下移、算"@ 会不会出圈"的样式和注释一并删掉 ——
  以后换图只换文件，不用再和字体度量较劲。
- 顺手修了移动端一个老毛病：**选完人之后 @ 浮层又弹回来**（`mobileText` 的 watch 是同步跑的，
  改值那一刻 `selectionStart` 还停在旧的 `@` 后面，于是重算触发词又判定"正在 @ "）；
  现在光标落到「@名字 」之后再重算一次，浮层真的收起。

### 为什么这样摆

原图的圆环刚好贴在画布四边（边缘被裁掉约 1.6px），所以让它**几乎占满按钮**时，图里的圆环正好压在按钮内沿上，
看起来就是「描边圆按钮 + 居中的 @」，和旁边几颗图标同一套语言；留 1px 是为了不让 50% 圆角把那圈细描边切出毛边。
换成"图标留 8px 边距"的旧写法会变成"圈里套圈"，@ 只剩 21px，太小。

### 怎么验（都跑过）

- `vue-tsc --noEmit` 0 error；`vite build` 出 `assets/index-B9PuQDVP.js` + `assets/icon_sns_chat_at-2vD4hUL1.png`。
- 真页面（无头 Edge + 真 UI，先建一个 2 人群聊）**桌面 1280×900**：**11 PASS / 0 FAIL**
  —— 按钮里确实是 `<img>`、图片加载成功（118×118 → 按钮 30px、图标 28.66px，四周留 1px）、圆角 50%、
  点一下弹成员列表（2 人）、选人后「@明河 」插进输入框、浮层收起。
- 同样一套在**移动 430×900** 下再跑一遍：**11 PASS / 0 FAIL**（按钮 36px、图标 34px）。
- 线上（`https://chat.peilika.beer` 换包后，用无头 Edge 真点一遍）：`index-B9PuQDVP.js`、
  图标 `nw=118` 加载成功、30px 按钮里 28.7px、浮层两名成员、选人插入正常。

### 怎么退

`git revert` 这个提交即可（纯前端素材 + 一个组件里的两处）；线上回滚就是把前端目录换回上一份
`/var/www/peilika-chat.old-<时间戳>`（见部署记录）。老客户端不涉及。

---

## 2026-11-21 15:50 · 「重新生成」被她当成「你叫了我两次」：重生成不再往历史里塞第二句

> 群友原话：「就我叫了她一声，从新生成一次后，她认为我叫了她两次就很奇怪」。

### 病根（两处，都在"重新生成"这条路上）

1. **客户端把同一句话又发了一遍。** 重新生成走的是"再发一条消息"，
   `client_msg_id` 每次都是新的随机串（`clientMsgIdFor` 里的 Map 在成功后就被清掉了），
   服务端的幂等键拦不住 → 库里真的多出一条一模一样的玩家消息。
2. **种子历史里混进了"正要发的那一句"。** `backendInput.history` 拿的是
   `conv.contextHistory` 的**同一个数组引用**，而 `sendUserMessage` 紧接着会往这个数组里
   push 玩家这句 —— 请求体是在那之后才组装的，于是"新消息"被当成历史导进服务端
   （`/v1/imports`），再作为新消息写一遍。

两者叠加的结果：服务端历史里出现两句一样的玩家话，模型只能理解成"你叫了我两次"。
生产库实测（`baker_back.messages`）：一个会话里同一句玩家话连着出现 6 次
（seq 31/33/35/37/39/41，每句后面跟一条不同的回话），逐次时间差 40–140 秒 —— 就是连点了 6 次重新生成。

### 改了什么

**服务端（新协议，老客户端不传就完全不变）**

- `schemas.MessageCreateRequest` 新字段 `regenerate: bool = False`。
- `api/conversations.py` 新增 `_regenerate_turn` / `_plan_regenerate` / `_clear_tail_for_regenerate`：
  - 正文等于**最后一条**玩家消息 → 删掉它之后的旧回复、把 `next_seq` 退回去，
    新回话**落回原来的 seq**（提问 → 回答 → 提问 → 回答 的顺序不断）；
  - 正文只匹配到**更早**的某一轮 → 库一动不动，回话只回给客户端（删后面几轮是真丢数据）；
  - 库里根本没有这句 → 按正常发消息处理（服务端还没收到过它）。
- `repos/messages.py` 新增 `last_user` / `find_last_user_by_text` / `delete_after_seq`。
- 流式端点 `messages:stream` 同一套判断（网页默认关流式，但开关一开就得对）。

**前端**

- `utils/backend.ts`：请求体带 `regenerate`；新增 `trimSeedHistory()` ——
  种子历史末尾若和"正要发的那一句"正文相同就剔掉（只从末尾剔，中间那句是真的说过）。
- `composables/useAiChat.ts`：`regenerate()` 走 `regenerate: true`；
  普通发送时 `history` 改成 `.slice()` 复制（引用别名是这个 bug 的上游）。

### 怎么验

| 验证 | 结果 |
|---|---|
| `pytest tests/test_regenerate.py`（7 条，含流式那条） | **7 passed** |
| 回归：`idempotency / continue / attachments / legacy / smoke / imports` | **64 passed** |
| `vue-tsc --noEmit` | 0 error |
| 真浏览器 e2e（无头 Edge + 真后端 + 记账假上游）`scripts/regen-e2e-cdp.mjs` | **12 PASS / 0 FAIL** |

e2e 断言的三件关键事：重新生成后服务端仍只有「玩家 + 角色」两条；玩家消息只有一条且正文没变；
**最后一次提示词里那句玩家话只出现一次**（这正是"她以为你叫了两次"的病根）。

### 怎么退

- 后端：`cp -a app/api/conversations.py.bak_<时间> app/api/conversations.py`（`schemas.py` / `repos/messages.py` 同理）+ `systemctl restart baker-api`；
- 前端：换回上一份 `/var/www/peilika-chat.old-<时间>` 目录。

### 一句真话

已经被写脏的老会话不会自动变干净 —— 那几句重复的玩家消息还在库里，
模型看历史时照样会看到。重生成现在不会再添新的，但要让某段老会话恢复正常，
要么新开一段，要么单独把那几条重复行清掉（要动的话先说一声）。

---

## 2026-11-21 · 群聊输入框的「@」按钮：先修看不见，再按站长要求放大加粗

> 两轮反馈：
> ① 「群聊at按钮是白底白图标，换成白底黑at图标」
> ② 「做大点（at直径占圆直径的90%），加粗」

### 改了什么

都在 `front/src/components/chat/ChatInput.vue` 的 `.chat-input__at`：

| 项 | 原来 | 现在 |
|---|---|---|
| 颜色 | `rgba(255,255,255,.82)` | `#1c1c1c` |
| 字号 | `18px` | **`43px`** |
| 字重 | `700` | **`900`** |
| 居中 | 行内流（靠基线） | 绝对定位 + `flex` 居中，再 `translateY(1px)` |

### 为什么是 43px（而不是 45px = 正好 90%）

圆形按钮直径 45px，90% = 40.5px。这个字体里 @ 的墨迹大约 **0.90em 宽 / 0.87em 高**，
但它的右下角是一圈很宽的弧——按字宽算 90% 时，那圈弧会**探出圆外**，看起来像被圆切掉。

所以按「整块墨迹是否还落在半径 22.5px 的圆内」做了逐像素测量（把按钮渲染到白底上再扫像素）：

| 字号 | 墨迹最远半径 | 出圈像素（4x 截图） | 结论 |
|---|---|---|---|
| 42px | 22.16px | 0 | 完全在圈内 |
| **43px** | **22.62px** | **24（≈1.5 css px²，肉眼无感）** | **采用** |
| 44px | 23.41px | 292 | 右下角明显探出 |

43px 的字宽 38.7px ≈ 直径的 **86%**，视觉上就是「满格」；再大就会看出破绽。
另外原来靠行内流基线居中，字号一大就会偏下，所以改成绝对定位 + flex 居中。

### 怎么验

```bash
cd front && npx vue-tsc --noEmit && npx vite build
grep -o 'chat-input__at[^}]*}' dist/assets/index-*.css
# → .chat-input__at[data-v-e6b256b0]{position:absolute;inset:0;display:flex;align-items:center;
#    justify-content:center;font-size:43px;font-weight:900;line-height:1;color:#1c1c1c;
#    transform:translateY(1px);pointer-events:none}
```

真页面复核（无头 Edge + CDP，把按钮用**编译后的 CSS**画在已部署的页面上再截图）：
`font-size:43px / font-weight:900 / color:rgb(28,28,28)`，45×45 圆内满格、四周不出圈。

线上（test3 与正式站各换一次包）：`index-DyenV9JS.js` → `index-CM-qHNqN.js` → **`index-D-CdaRr_.js`**；
`https://chat.peilika.beer/` 200、`/readyz` 200。

### 怎么退

```bash
sudo bash -c 'mv /var/www/peilika-chat /var/www/peilika-chat.bad \
  && mv /var/www/peilika-chat.old-20261121_141721 /var/www/peilika-chat'
# test3 同理：peilika-chat-test3.old-20261121_141721
```


---

## 2026-11-20 · 群反馈修复（指定发言 / 不出话提示）+ 数据存储改成「本地优先」

> 两件事合成一轮：先按 `docs/bug-2026-11-20.md` 修掉群里最直观的两个 bug，
> 再按老板口径「**优先存储/读取在本地的数据，用户有需要时登录，自动上传至服务器**」
> 把存储模型调头。
>
> **这一轮只在本地 WSL 验过，没有上线**（老板原话：「在本地wsl运行验证」）。

### 改了什么

**① 指定发言指定的人不接话（B1）**

| 位置 | 改动 |
|---|---|
| `back/app/group.py` | 新增 `match_member()`：slug **和**中文名都认，还容前后空格 / 大小写；`_decide_speaker` 改用它 |
| `front/src/utils/groupBackend.ts` | 新增 `assignSlug()`：发请求前把中文名翻成 slug（翻不出来才发原名，老服务端也不会坏） |

老客户端（EXE / APK / 缓存里的旧 JS）发的是中文名，只改前端等于「新用户好了、老用户照旧」——
所以容错放在后端，两边都认。

**② 点「指定发言」没反应（B2）**

- `composables/useGroupChat.ts`：`speakAssigned()` 返回「这一趟有没有说出话来」（比对消息条数）。
- `components/chat/GroupFlowControl.vue`：没出话就提示「这一轮已经说完了。先发一句话开新一轮，再点「指定发言」」；没选人 / 上一轮还在跑也各给一句。

**③ 存储改成「本地优先」（核心）**

| 位置 | 改动 |
|---|---|
| `composables/useChatPersistence.ts` | **删掉**「数据源是 server 就不写本地卡库」的跳过逻辑 —— 本地从此永远有一份完整副本 |
| `utils/conversationSync.ts` | 新增纯函数 `mergeServerCards()`：云端**只补不改**（本机独有的东西一条不删），并按「消息条数 == next_seq-1」认领没有 remoteId 的老存档 |
| `stores/chat.ts` | 新增 `applyMergedList()`：装合并结果，保住选中、保住折叠状态 |
| `stores/list.ts` | `refresh()` 改成合并；新增 `hydrateIfEmpty()`（本机空才去云端捞）、`onLoggedIn()`（登录 → 自动上传 → 云端补齐）、`uploadLocalNow()` |
| `utils/listSource.ts` | 默认数据源恒为 `local`；切云端不再有门槛（合并只加不改，没风险了）；登录不再自动换视图 |
| `utils/cloudPolicy.ts`（新） | 纯函数 `planUploadOnLogin()`：没登录不传 / 没记录不传 / 同一个账号只传一次（重复整包会在云端建**重复会话**） |
| `utils/cloudSync.ts`（新） | 上传编排 + marker（`baker_cloud_upload_v1`，按账号记） |
| `stores/account.ts` | 「有记录就必须先登录」那道墙**默认关掉**（`FORCE_LOGIN_WALL = false`，判据留着） |
| `components/layout/SettingsDialog.vue` | 这一行改叫「数据存储」：`本机（默认）` / `本机 + 云端`，提示文案跟着改 |

**④ 顺手修掉两个底层 bug（不修的话 ③ 会出事）**

- `utils/zipExport.ts`：`sanitizeCards` 是持久化恢复与导入导出**共用**的白名单，它把
  `remoteId / remoteTime / remoteNextSeq` 全丢了 —— 于是每次重开，云端会话在本地都像"新的"，
  合并会一遍遍追加（实测重开两次：36 张占位卡 + **72 段会话**）。现在这些字段跟着落盘，
  导出包仍由新增的 `stripRemoteRefs()` 抹掉（会话 id 不该跟着导出包流出去）。
- `stores/list.ts`：判断「本机是不是空的」原来数的是**全部会话**，可 cards 恒含 36 张内置角色卡、
  每张都挂一段空占位对话 → 条件永远不成立，"云端补齐"从来不会触发。改成数**有消息的**会话。

**⑤ 测试**

- 新增 `back/tests/test_group.py` 4 条（中文名 / slug / 空格大小写 / `/next` 也认中文名，外加纯函数 `match_member`）。
- `front/scripts/list-e2e.mjs`：[8][9] 的门槛断言按新口径重写，新增 [9b] 合并只补不删 6 条、[9c] 自动上传边界 6 条。
- 新增 `front/scripts/storage-e2e-cdp.mjs`：用无头 Edge（CDP）在**真浏览器**里跑「默认本机 → 云端补齐落盘 → 切云端再加一段 → 断掉后端仍能看到记录」9 条。

### 为什么

线上「我记录怎么没了」有三条机制（换域名 / 设备身份丢 / 崩溃时列表拉不到），
但代码层面有一条共同的放大器：**server 模式本地零副本**。
所以这次不是补一句提示，而是把"谁是真相"换掉 —— 本机是真相，云端是备份与补齐。
登录从"必须做的前置"变回"有需要才做的动作"；登录后自动把本机存档传上去，用户不用再找按钮。

### 怎么验（全部在本地 WSL，没有碰生产）

```bash
# 环境：WSL Ubuntu 24.04 / Python 3.12 / 独立 MySQL 8.0（3307）/ venv ~/bakerai-dev/venv
# 依赖按 back/requirements.lock 装（cp312 轮子全有）

# 1) 后端单测（群聊这一块，含本轮新增的 4 条）
cd ~/bakerai-dev/src/back && ~/bakerai-dev/venv/bin/python -m pytest tests/test_group.py -q
# → 12 passed

# 2) 真后端 + 假上游，验「指定发言」（这就是 B1 的现场）
bash /mnt/f/dshworkspace/BakerAI/_audit/wsl_live_smoke.sh
# readyz 200；建群 assign（佩丽卡 / 陈千语）
# ① 中文名：assign_speaker=陈千语   → speaker=陈千语   ✅
# ② slug  ：assign_speaker=perlica → speaker=佩丽卡   ✅（老客户端不变）
# ③ /next ：speaker=佩丽卡          → speaker=佩丽卡   ✅
# ④ 单聊回归                        → reply 正常      ✅
# 日志里「指定的发言人（…）不在群里」= 0 条

# 3) 前端纯函数 / 契约（Node 直接跑，不需要浏览器）
cd ~/bakerai-dev/src/front
node scripts/list-e2e.mjs            # 49 PASS / 0 FAIL
node scripts/claim-smoke.mjs         # 39 项通过
node scripts/legacy-migrate-smoke.mjs# 22/22
node scripts/two-archives-e2e.mjs    # 10 PASS
BAKER_BACKEND=http://127.0.0.1:15660 node scripts/contract-smoke.mjs   # 27 PASS（老客户端契约）
BAKER_BACKEND=http://127.0.0.1:15660 node scripts/adapter-e2e.mjs      # 16 PASS

# 4) 真浏览器（无头 Edge + CDP）验存储接线
node scripts/storage-e2e-cdp.mjs     # 9 PASS / 0 FAIL（Edge 无头 + CDP，跑在 WSL 静态站上）
#   [1] 首次进入 baker_list_source=local（默认本机）        本地 36 张卡 / 36 段会话
#   [2] 服务端建一段会话 → 重载后它出现在 IndexedDB 里     36 → 36（占位被真会话取代）
#   [3] 切「本机+云端」→ 再建一段 → 两段同时在本地         36 → 37
#   [4] 把后端地址指向死端口再重载 → 两段仍在（本地优先）   2 段带 remoteId

# 5) 类型与构建
cd front && npx vue-tsc --noEmit   # 0 error
npx vite build                     # ✓ built（dist 重新出来，未部署）
```

### 本次实测汇总（2026-11-21 00:0x）

| 项 | 结果 |
|---|---|
| `pytest tests/test_group.py`（含新增 4 条） | **12 passed** |
| 真后端 + 假上游（B1 四种调用） | 中文名 / slug / `/next` / 单聊 **全对**，「指定的发言人不在群里」= 0 条 |
| `list-e2e.mjs` | **49 PASS / 0 FAIL** |
| `claim-smoke` / `legacy-migrate-smoke` / `two-archives-e2e` | 39 / 22 / 10 全过 |
| `contract-smoke` / `adapter-e2e`（老客户端契约，打真后端） | **27 PASS / 16 PASS** |
| `storage-e2e-cdp.mjs`（无头 Edge 真浏览器） | **9 PASS / 0 FAIL** |
| `vue-tsc --noEmit` / `vite build` | 0 error / ✓ built |
| 后端其余相关文件（group_next / smoke / continue / suggestions / time_awareness / schema / imports） | **58 passed** |
| legacy_group / legacy_upload / memories / memory（单文件跑） | 全过 |

> ⚠️ **全量 `pytest` 在 WSL 上跑不完**（约 45% 后不再前进，单文件逐个跑都过）；
> 换到 test3（MySQL 8.4 / Python 3.14）一次跑完了：**288 passed / 10 error / 0 failed**。
> 那 10 个 error 全在 `tests/test_prompt_v2.py`，原因一致：它要读老包的
> `/home/原始网站前后端/backend/characters/characters_v2`（36 个 v2 角色 json），
> 这台机器上没有这个目录（`files: 0`）—— 是**缺素材**，与本轮改动无关；
> 把那份老包素材放回去，这 10 条就能跑。

> 跑的是 **Python 3.12**（服务器是 3.14）：依赖按 `requirements.lock` 装，cp312 轮子全有；
> 代码层面没有 3.13+ 语法（ruff 的 target 本来就写的 py312）。上线前仍需在 3.14 上复跑一次。

### 怎么退

```bash
cd F:/dshworkspace/BakerAI/BakerAI_new/web
git revert <这一轮的 commit>        # 后端 2 个文件 + 前端 11 个文件 + 1 个新验证脚本
```

本地 WSL 环境本身不碰任何线上资源：`rm -rf ~/bakerai-dev` 就干净了
（MySQL 是独立实例 3307，系统那个 3306 没动过）。

### 2026-11-21 00:10 上 test3（预演站，正式站仍未动）

老板指令：「上test3」。按"先备份 → 变更 → 四项验证"走的，全过程只碰 `baker-v4-test3`。

**部署了什么**

| 目标 | 内容 | 校验 |
|---|---|---|
| `~/baker-v4-test3/back/app/group.py` | B1 的后端修复 | sha256 `ea285c58…`（与本地一致）；`compileall` 通过 |
| `~/baker-v4-test3/back/tests/test_group.py` | 新增的 4 条测试 | sha256 `e68d3f98…` |
| `/var/www/peilika-chat-test3` | 新前端整包（144 个文件） | `index.html` → `assets/index-BdZOQXuc.js`，1,312,848 B |

**验证（全部在 test3 上真跑）**

```
systemctl --user is-active baker-api-test3   → active（NRestarts=0）
curl 127.0.0.1:5661/readyz                   → {"ok":true,...migrations":"019_prompt_variant"}
pytest tests/test_group.py（Python 3.14.4 / MySQL 8.4）  → 12 passed
真打一次"指定发言"（消耗 2 次上游调用）：
  ① assign_speaker=陈千语  → speaker=陈千语   ✅（这就是线上那个 bug）
  ② assign_speaker=perlica → speaker=佩丽卡   ✅（老客户端不变）
  日志「指定的发言人…不在群里」= 0 条
真浏览器（无头 Edge）跑 storage-e2e-cdp.mjs  → 9 PASS / 0 FAIL
  （指向 https://test3.peilika.beer，默认本机 / 云端补齐落盘 / 只加不改 / 断网仍可读）
公网：https://test3.peilika.beer/ → 200；/v1/meta → 200；/v1/admin/overview（无 token）→ 401
```

**怎么退**

```bash
# 后端（部署前的原件我留在同目录了）
cd ~/baker-v4-test3/back && cp -a app/group.py.bak_pre_20261121 app/group.py
systemctl --user restart baker-api-test3

# 前端（原子换回的目录还在）
sudo bash -c 'rm -rf /var/www/peilika-chat-test3.bad \
  && mv /var/www/peilika-chat-test3 /var/www/peilika-chat-test3.bad \
  && mv /var/www/peilika-chat-test3.old-20261121_000936 /var/www/peilika-chat-test3'
```

**遗留**：验证过程在 test3 库里留了一个设备、2 段空会话（`上线验收集群` 里还有 2 条真消息）、2 条单聊，都是那个 `platform=deploy-check` / 浏览器匿名设备的，跟真用户无关；要清就按 `devices.platform` 找。

### 2026-11-21 13:58 上正式站（chat.peilika.beer / 5660）

老板指令：「推正式站」。test3 验完后原样推 **chat** —— 三个部分都换，顺序照旧：先备份、再改、立刻验。

**部署了什么**

| 目标 | 内容 | 校验 |
|---|---|---|
| `~/baker-v4/back/app/group.py` | B1 后端修复 | 覆盖前已备份 `app/group.py.bak_20261121_135701`（sha `e96fc52…`）；新文件 sha `ea285c58…`；`compileall` 过 |
| `~/baker-v4/back/tests/test_group.py` | 新增 4 条测试 | sha `e68d3f98…` |
| `/var/www/peilika-chat` | 新前端整包（144 文件） | `index-BARckGOU.js` → **`index-BdZOQXuc.js`**（1,312,848 B）；旧目录留在 `/var/www/peilika-chat.old-20261121_135835` |

`baker-api`（5660）重启一次：`active`、`NRestarts=0`、`/readyz` 200。

**验证（生产上真跑）**

```
pytest tests/test_group.py（生产目录 / Python 3.14 / MySQL 8.4）  → 12 passed，0 failed
真打一次「指定发言」（消耗 1 次上游调用）：
  assign_speaker=陈千语  → speaker=陈千语   ✅
  日志「指定的发言人…不在群里」= 0 条
公网：https://chat.peilika.beer/ 200；/readyz 200；/v1/meta 200；
      /v1/admin/overview（无 token）401；新 JS 200 / 1,312,848 B（与本地构建同尺寸）
```

> 旧 JS `assets/index-BARckGOU.js` 在**源站**已经 404，但公网还 200 —— 那是 Cloudflare 边缘缓存。
> 这反而是好事：刚打开页面、还拿着旧 index.html 的人，不会被卡在"JS 加载失败"上。
> 而且**后端已认中文名**，所以这些仍在跑旧 JS 的人点「指定发言」也会立刻正常（不用等他们刷新）。

**怎么退**

```bash
# 后端
cd ~/baker-v4/back && cp -a app/group.py.bak_20261121_135701 app/group.py
systemctl --user restart baker-api

# 前端（原子换回）
sudo bash -c 'rm -rf /var/www/peilika-chat.bad \
  && mv /var/www/peilika-chat /var/www/peilika-chat.bad \
  && mv /var/www/peilika-chat.old-20261121_135835 /var/www/peilika-chat'
```

**生产库新增的验证数据**：一个 `platform=deploy-check` 设备、一段群聊「上线验收-指定发言」（1 条真消息）。要清就按 `devices.platform` 找。

### 还没做

- **正式站已上**（2026-11-21 13:58，见上一节）；**面板（admin.peilika.beer）没动** —— 这一轮没有后台改动。
- **C1（重新生成抽风）/ D1（服务端模式"感觉怪"）**：仍然缺现象，按 `bug-2026-11-20.md` §3/§4 先要证据再动手。
- 自动上传是「按账号传一次」：之后在**自定义 API 模式**下新聊的内容仍只在本机（走默认后端的内容云端本来就有，不用再传）。

---

## 2026-11-20 · 后台「今日 / 按天」改成东八区日界（原来是 UTC）

> 老板看后台截图：「改为utc+8时区而不是utc」。他凌晨 00:39 打开总览，
> 「今日用量」显示 1,565 次 —— 那是 **UTC 的今天**（= 东八区 11-19 08:00 到现在），
> 把昨天大半天都算进了"今天"；直方图的最后一根柱子也停在 11.19。

### 改了什么

**后端（3 个文件）**

| 文件 | 改动 |
|---|---|
| `app/clock.py` | 新增 `STATS_TZ_OFFSET_HOURS = 8` 与 `day_start_utc(days_back=0)`：把"东八区某天零点"换算成 naive UTC |
| `app/repos/admin.py` | `usage_by_day()` 分组从 `date(created_at)` 改成 `date(created_at + INTERVAL 8 HOUR)`，抽出 `_day_label()` |
| `app/api/admin.py` | `/admin/overview` 与 `/admin/usage` 的边界改用 `day_start_utc()`；顺手删掉不再用的 `datetime`/`timedelta` 导入 |

**前端（面板 4 个文件 + 重新构建）**

- `src/lib/format.ts`：`utcDayKey`/`lastUtcDays` → `statsDayKey`/`lastStatsDays`（补零时同样 +8 小时，否则最后一根柱子会错位一天）。
- `src/views/OverviewView.vue`：「按天 · UTC 日界」→「按天 · 东八区日界」，「UTC 零点起」→「东八区零点起」，页脚说明重写。
- `src/views/UsageView.vue`：表头「日期（UTC）」→「日期（UTC+8）」，页头说明改口径。
- `src/lib/types.ts`：`UsageDay.date` 的注释改成东八区日。

**新增测试** `tests/test_admin_day_boundary.py`（4 条）：东八区零点换算、`days_back` 走整天、15:59 UTC 与 16:00 UTC 落在两个东八区日、今日边界能挡住前一天。

### 为什么

库里存的是 naive UTC（这是全项目的约定，不动），但人都在东八区。
照 UTC 切天，东八区凌晨 0–8 点的请求会被算进"前一天"，站长看后台和自己的表对不上。
只改"按天统计"这一处的边界 —— 写库、比大小、算耗时仍然一律 UTC。

### 怎么验

```bash
# 1) 单元/接口（服务器上跑，MySQL 8.4）
cd ~/baker-v4-test3/back
~/baker-v4/venv/bin/python -m pytest tests/test_admin_day_boundary.py tests/test_admin.py -q
# → 4 passed + 21 passed

# 2) 生产库实测（只读，脚本用依赖覆盖假装管理员，不写一行）
cd ~/baker-v4/back && ~/baker-v4/venv/bin/python scripts/verify_day_boundary.py
# 东八区今天零点 = UTC 2026-11-19 16:00:00
# today.requests  接口=192  SQL 手算=192        ← 一致
# 对照 · 同一批行按 UTC 切天，'今天'会算成 1630 条（多吞 1438 条本该属于昨天的）
# days（接口）=[11-18:1522, 11-19:2149, 11-20:192] == days（SQL）
# 结论： PASS
```

面板：`https://admin.peilika.beer/` → 200，引用 `assets/index-C4s30o3h.js`（119,406 B，与本地构建逐字节同尺寸）；
未带 token 打 `/v1/admin/overview` → 401（路由活着）。

### 怎么退

```bash
# 后端（3 个文件）
cd ~/baker-v4/back
for f in app/clock.py app/repos/admin.py app/api/admin.py; do cp -a $f.bak_20261120_004549 $f; done
systemctl --user restart baker-api && systemctl --user restart baker-api-test3

# 面板（静态站，原子换回）
sudo bash -c 'rm -rf /var/www/panel.bad && mv /var/www/panel /var/www/panel.bad && mv /var/www/panel.old-20261120_004708 /var/www/panel'
```

> 注意：回滚后旧的 `index-_OQQC0xf.js` 会跟着 `panel.old-*` 一起回来，刷新即可。

---

## 2026-11-19 · test3 的全部改动推上正式站（chat.peilika.beer）

> 老板本人指令：「test3全部新更改推chat」。土豆的冻结在这一刻解除，以下是推的东西。

### 推了什么

**后端 5 个文件**（与本地 sha256 逐个核对一致，无新增文件、无数据库迁移）：

| 文件 | 内容 |
|---|---|
| `app/api/auth.py` | 新增 `GET /v1/auth/owner-data`（只回会话/消息数） |
| `app/repos/conversations.py` | 新增 `count_for_owner()`（两条 COUNT） |
| `app/schemas.py` | 新增 `OwnerDataOut` |
| `app/upstream.py` | 429 文案整理（流式分支不再把上游 JSON 甩给用户） |
| `app/settings.py` | 429 冷却 60s → 180s |

**前端**：`index-BARckGOU.js`（整包），含三件事：
① 3.2.2 开屏（随机干员 + 名字 + 大招语音）；② 「有记录就必须先登录」那道墙；
③ 同一角色两段对话互相串的修复。

### 操作方式（可回放）

```bash
# 1) 备份
cp -p <每个文件> <文件>.bak_20261119_222329
tar czf ~/backup-web-chat-before-20261119_222329.tgz -C /var/www peilika-chat

# 2) 后端：scp 到 ~/tmp → cp 覆盖 → compileall → systemctl --user restart baker-api
# 3) 前端：解包到 /var/www/peilika-chat.new → mv 旧→.old-20261119_2225 → mv 新→正式（原子换）
```

### 验收（正式站实测）

```
index.html           200，assets/index-BARckGOU.js = 1,306,937 B（与本地构建同尺寸）
/v1/meta             200
/v1/auth/owner-data  401（无 token，说明路由已生效）
开屏                 能看到启动层，播完自动退场
全新浏览器           不拦登录（没有记录的新人照旧直接用）
有记录的设备         owner-data 1/1 → 被拦，墙标题「这段聊天记录还没挂到账号上」
/api.peilika.beer/chat  老客户端契约 200（chat 域本来就没有 /chat 反代，别在那边测）
重启后日志           200×125 / 201×7 / 204×1 / 401×2，**5xx = 0**，NRestarts=0
```

### 回滚（一条命令）

```bash
sudo bash -c 'mv /var/www/peilika-chat /var/www/peilika-chat.bad && mv /var/www/peilika-chat.old-20261119_2225 /var/www/peilika-chat'
# 后端：把 5 个 .bak_20261119_222329 覆盖回去 + systemctl --user restart baker-api
```

---
## 2026-11-19 · 「同一个角色的两段对话互相串」修好了

### 症状（群友权倾浊酒澄吾心，00:26）

> 第一个对话我叫了一声亲爱的，第二个对话的关系没有那么近，结果被直接无缝接

关了「记忆互通」也一样，100% 复现。**跟记忆开关无关** —— 那个开关管的是人情账
（`character_memories`），这条路上根本没走到那一步。

### 根因（三处，叠在一起）

1. **绑定只认角色**：`baker_v1_bindings` 的键是 `characterId`，
   于是同一角色的第 1 段和第 2 段对话**共用同一条服务端会话**。
   源码顶部当时把这写成"已知取舍"，但对用户就是 bug。
2. **会话失效重建时丢键**：流式失败 / `no_conversation` 会重建绑定，
   那次调用没带"是哪段对话"，于是按角色重新猜 → 又猜回第一段。
3. **导入历史后按角色认领**：`findConversation` 取"该角色 next_seq 最大的那条"，
   两条撞平时排序会**稳定地选回最早那条** → 第二段存档被绑到第一段。

### 改法

| 文件 | 改动 |
|---|---|
| `front/src/types/chat.ts` | `Conversation` 新增 `localId`（每段对话的稳定 id） |
| `front/src/utils/localConversation.ts` | **新增**：`newLocalId` / `ensureLocalId` / `conversationKeyOf`（服务端模式用 `r:<会话 id>`，本地模式用 `localId`） |
| `front/src/utils/backend.ts` | 绑定键 → `角色#对话id`；老绑定**只认领一次**（认领后删旧键，历史不丢）；导入后取"这次新冒出来的那条"；`PreparedTurn` 带上 `conversationKey`，重建绑定时一起带 |
| `front/src/composables/useAiChat.ts` | 发送 / 重新生成 / 图片 / 推荐四条路径统一带 `conversationKey` |
| `front/src/stores/list.ts` | 服务端列表点选时按 `r:<会话 id>` 写绑定（与发送同键） |
| `front/src/utils/zipExport.ts` | `localId` 进持久化白名单（漏了它每次重启都换 id → 会不断新建会话） |
| `front/scripts/two-archives-e2e.mjs` | **新增**回归：假后端验 10 条（两段对话两条会话 / 上下文隔离 / 第二轮归位 / 老绑定只认领一次 / 绑定表无纯角色键） |

### 验收

```
node scripts/two-archives-e2e.mjs   10 PASS / 0 FAIL
node scripts/list-e2e.mjs           35 PASS / 0 FAIL
node scripts/claim-smoke.mjs        39 项通过 / 0 项失败
vue-tsc --noEmit                    0 错
```
test3 上：`boot-check` 9/0、`wall-e2e` 11/0，部署 `index-BARckGOU.js`。

### 老用户会不会丢历史

不会：升级后第一次在**某个存档**里发消息，会把原来"按角色"的旧绑定**认领给这段对话**
（用一次就删掉旧键），所以当前在用的那段历史还在；另一段存档从此自己建会话。

> ⚠️ 已知边界：如果用户在升级后**先用另一段（空）存档发言**，那条老会话会被它认领
> （这是"谁先说话谁继承"的取舍）。真出问题可以人工把绑定改回去，数据本身没动。

---

## 2026-11-18 · 上游 429 不再把一整坨 JSON 甩到用户脸上

### 是什么

有人截图：气泡里躺着

```
上游 HTTP 429: {"error":{"message":"今日文本额度已用完，当前处于限速模式：
每 3 分钟可请求 1 次，请于 23:05:11 后重试。 (request id: 2026...)"}}
```

两个问题叠在一起：

1. **上游真的限速了** —— Agnes AI 的 key 当天文本额度用完，之后只允许每 3 分钟 1 次。
   这是**按 key** 的（同一时刻换一把 key 能正常回话，实测），不是整站挂掉。
2. **我们的锅**：后端有两条调用路径，只有非流式那条走了「整理成人话」的函数
   （`_error_text`）；**流式那条（网页聊天走的就是它）直接把原始响应体拼进错误里**。
   所以用户看到的是一段 JSON。

### 改了哪些文件

| 文件 | 改动 |
|---|---|
| `back/app/upstream.py` | 流式分支改用 `_error_text(response, body)`；429 时把原始响应体写进 `logger.warning`（给运维看）；新增 `_retry_clock()`，从上游文案里抠出「请于 23:05:11 后重试」→ 只展示到分钟 |
| `back/app/settings.py` | `key_cooldown_seconds` 60 → **180**：上游自己按 3 分钟算窗口，冷却 60 秒撑不到下个窗口，下一分钟又撞一次，每撞一次废掉一个用户的请求 |
| `back/tests/test_upstream_error_text.py` | 新增 5 条单测（真 429 原文 / 无时刻兜底 / 流式传 body / 其它状态码 / 时刻解析合法性） |

### 用户现在看到什么

| 情况 | 文案 |
|---|---|
| 429 且能解析出时刻 | `上游今日额度已用完，请于 23:33 后再试` |
| 429 解析不出时刻 | `上游限速（429），请稍等再发` |
| 其它 4xx/5xx | `上游 HTTP 502：bad gateway`（截断 200 字） |

### 验收

- 单测：`pytest tests/test_upstream_error_text.py` → **5 passed**
- test3 实测（23:33，额度窗口还没到）：
  `POST messages` → 502，气泡文案
  `话已经存下了，但上游没回：上游今日额度已用完，请于 23:33 后再试`
  —— 没有一个 `{`，也没有 request id
- 时刻过去后再发 → 200，正常回复

---

## 2026-11-18 · 开屏换回 3.2.2「随机干员 + 名字 + 大招语音」那套

### 是什么

土豆 09:46 说「我那加载动画呢，带角色名称的」——就是它。新前端之前用的是
一个极简开屏（`//BAKER/` 字样 + 一根进度条），现在换成 3.2.2 的原版：

1. **启动层在主包下载之前就显示**（纯 HTML/CSS + 一段内联脚本，不是 Vue 组件）；
2. 随机一名干员（33 位），**大字报出名字** + 立绘剪影；
3. 每 2 秒换一句这个干员的大招语音（「猎物，一个不留！」「让你挑食！」…）；
4. 进度条假推进到 92% 停住，挂载完成后从**当前值**平滑补到 100；
5. 黄色竖条扫过 → 淡出 → 从 DOM 移除，全程约 1.9 秒；
6. 移除后广播 `baker:boot-done`，公告 / 提醒 / 更新 / 首次偏好都等这个信号；
7. 进入主体 1.2s 后，低优先级把剩下 32 张剪影灌进缓存（下次随机到谁都是秒出）。

### 改了哪些文件

| 文件 | 改动 |
|---|---|
| `front/index.html` | **换成 3.2.2 的版本**：启动层标记 + 版式 CSS（`--u` 基准单位）+ 内联驱动脚本（33 位干员的语音表） |
| `front/public/splash/` | 新增 453KB 资源：33 张剪影 + 横竖两张背景 + deco/triangles 两个 svg |
| `front/src/main.ts` | 挂载后调 `__bakerBoot.exit()`（补满进度 → 擦幕 → 淡出 → 移除） |
| `front/src/App.vue` | 去掉旧的开屏组件 `<SplashOverlay>`；改成监听 `baker:boot-done` 来放行弹窗 |
| `front/src/easteregg/moon/MoonEgg.vue` | 开屏判据从 `.splash-overlay` 改成 `.splash-overlay, #boot-layer`（彩蛋不会在开屏底下冒出来） |

`components/splash/SplashOverlay.vue` 文件保留但不再引用（想换回去改两行即可）。

### 验收（都在公网 test3 上）

```
boot-check.mjs   9 PASS / 0 FAIL
  · 主包下载期间能看到启动层            （冷加载时约 5 秒，足够读完名字）
  · 带干员名字 / 大招语音 / 进度数字     （弧光 ·「避无可避。」· 4%）
  · 立绘真的解码出来（naturalWidth>0）
  · 自己退场并从 DOM 移除、控制台 0 报错
wall-e2e.mjs    11 PASS / 0 FAIL   ← 登录墙回归，没被开屏影响
```

桌面 1440×900 与手机竖屏 390×844 都截图确认过（`boot-live.png` / `boot-mobile.png`）。

> 剪影是 3.2.2 原版就带的**白色剪影占位**（不是彩色立绘），和原版观感一致；
> 想换成真立绘，把 `public/splash/portraits/<干员名>.webp` 换掉即可，代码不用动。

---

## 2026-11-18 · 预演站 test3.peilika.beer（数据完全隔离）

### 是什么

`https://test3.peilika.beer` = **新前端 + 新后端 + 自己的库**，和正式站互不干扰：

| 项 | 正式站 chat | 预演站 test3 |
|---|---|---|
| 前端 root | `/var/www/peilika-chat` | `/var/www/peilika-chat-test3` |
| 后端端口 | 5660（`baker-api`） | **5661（`baker-api-test3`）** |
| 程序目录 | `~/baker-v4/back` | `~/baker-v4-test3/back` |
| 数据库 | `baker_back` | **`baker_back_test3`** |
| 日志 | `~/baker-v4/logs/baker-api.log` | `~/baker-v4-test3/logs/baker-api-test3.log` |

### 数据隔离的做法（重要）

只从生产库拷了 3 张"重造不了"的基础表：`characters`(36) / `worldbook`(147) / `api_keys`(47)。
**`users` / `devices` / `conversations` / `messages` 一张都没拷** —— 预演站上不存在任何
真实用户的聊天记录（实测 `users=0 conversations=0 messages=0`）。要测登录墙，自己注册一个号就行。

### 装法（三个脚本，可重放）

脚本就放在仓库里：`deploy/test3/`（另有 `wall-e2e.mjs` 是那道墙的浏览器验收）

```
bash deploy/test3/setup_y.sh          # 以 y 身份：解包代码、写 .env(5661)、准备目录
sudo bash deploy/test3/setup_root.sh  # 建库+授权、铺 dist 到 /var/www、装 nginx 站点、reload
bash deploy/test3/setup_run.sh        # 迁移建表、装 systemd 用户单元 baker-api-test3、起服务
sudo bash deploy/test3/copy_data.sh   # 迁移之后拷那 3 张基础表（--replace，盖掉种子数据）
```

服务器上的副本在 `~/tmp/`（上传前要先 scp 上去，脚本里写的是 `~/tmp/xxx.tgz`）。

nginx 站点是 `/etc/nginx/sites-available/test3-peilika`，**故意不 include**
`bakerai-new-api.conf`（那份写死 5660 = 生产），反代直接写在站点里打 5661。

### 验收

- `https://test3.peilika.beer/` → 200，`index-B9N5ZfJT.js`
- `/v1/meta` 走 5661 → 200（`product:lite`）
- `https://test3.peilika.beer` 上跑 `_runtime/login-wall/wall-e2e.mjs` → **11 PASS / 0 FAIL**
- 真发一句话：`POST messages (generate=true)` → 200，4.6s 出回复（上游 key 可用）
- 同一时刻正式站仍是 `index-QqzQdOz9.js`，`baker-api`(5660) 未重启

⚠️ 用脚本直接打 `https://test3.peilika.beer` 会被 Cloudflare 拦（`error code: 1010`，
按 UA 判定）。要么带正常浏览器 UA，要么走 `127.0.0.1` + `Host:` 头从服务器本机测。

---

## 2026-11-18 · 有聊天记录的人，进站必须先登录或注册

### 一句话

以前聊天记录挂在「这台设备」的浏览器里，清缓存 / 换浏览器 / 换手机就没了；
现在**只要这台设备上还有记录、而你没登录账号**，进站先让你登录或注册。
登录那一刻，那批记录会自动挂到你账号名下 —— 换任何设备都还在。
**其他一切保持不变**（列表、迁移、备份、旧版模式都没动）。

### 改了哪几个文件

| 文件 | 作用 | 风险 |
|---|---|---|
| `back/app/api/auth.py` | 新增 `GET /v1/auth/owner-data`，只回「名下有几段会话 / 几条消息」 | 只读接口，不改任何数据 |
| `back/app/repos/conversations.py` | 新增 `count_for_owner()`，两条 `COUNT` 查询 | 只读 |
| `back/app/schemas.py` | 新增 `OwnerDataOut`（两个整数字段） | 无 |
| `front/src/components/layout/LoginWall.vue` | **新增**：那道墙（登录 / 注册表单 + 为什么必须登录） | 新文件，独立渲染，不碰既有组件 |
| `front/src/stores/account.ts` | 新增 `checkOwnedHistory()` / `needLoginWall` | 只加状态，登录/注册逻辑原样复用 |
| `front/src/utils/auth.ts` | 新增 `fetchOwnerData()` | 一个 GET |
| `front/src/App.vue` | 启动时问一次记录数；`needLoginWall` 为真时挂上墙 | 挂载点一处 |
| `front/src/main.ts` | 删掉重复的「问我是谁」，挪到 App.vue 一起做 | 少一次重复请求 |

### 五条行为边界（都验过）

1. **新用户不受影响**：名下 0 段会话 → 不出墙，打开就能聊。
2. **拉不到数量就不拦人**：接口失败按「没有记录」处理。网络抖动不能把整站挡在门外。
3. **已登录的人不会看到墙**：先确认「我是谁」，再查记录数，不会误拦自己。
4. **不给「以设备身份继续」的出口**：那等于允许数据继续裸奔在设备上。
   真卡住（设备被别的账号占着）时给一句明确出路：用原账号登录，或去群里找管理员。
5. **老客户端（EXE / APK）一行代码都没动**：它们走的是 `/chat` 那批老接口。

### 验收（本地真浏览器 + 真后端，11 条断言）

```
node _runtime/login-wall/wall-e2e.mjs
  PASS  全新浏览器不出墙
  PASS  有记录的设备出现墙
  PASS  标题是"还没挂到账号上"
  PASS  文案里有数量（这台设备上有 1 段对话（共 3 条消息））
  PASS  两个页签都在（登录/注册）
  PASS  墙盖在界面上（命中 .lw）
  PASS  注册成功后墙消失
  PASS  注册后记录仍在（adopt 生效）  · 1 段 / 3 条
  PASS  token 现在是账号身份
  PASS  已登录刷新不再出墙
  PASS  控制台无报错
合计：11 PASS / 0 FAIL
```

配套回归（没被我改坏）：

- `front/scripts/list-e2e.mjs` → **35 PASS / 0 FAIL**
- `front/scripts/claim-smoke.mjs` → **39 项通过 / 0 项失败**
- `front` 类型检查（`vue-tsc --noEmit`）→ **0 错**

### 怎么自己起本地环境验

```bash
# 1) 数据库 + 后端（WSL 里，5660）
bash BakerAI_new/_runtime/wsl_backend_db.sh
bash BakerAI_new/_runtime/wsl_backend_run.sh

# 2) 前端 dev（5173，/v1 反代到 5660）
cd BakerAI_new/web/front && npx vite --port 5173

# 3) 浏览器打开 http://localhost:5173/
#    想看成"有记录"的样子，把某个有数据的设备 token 写进 localStorage：
#    localStorage.setItem('baker_device_token', '<token>')
```

### 怎么退

- 代码：`git revert` 这一提交，或者直接删 `LoginWall.vue` + 回滚 `App.vue` 的挂载那两行。
- 线上：这次**没有部署**，正式站仍是原样。

### 还没做的（按整改清单排在后面）

- 「默认本机存档」：**故意没做**。直接一刀切成 local，430 段匿名会话的拥有者
  下次打开会看到空列表 —— 那是第二次事故。要按 `问题总结.md` §12.3④ 的条件做。
- 去掉迁移弹窗、旧版模式、迁移条数断言、备份只读断言、owner_key 隔离断言：排队中。
- ⚠️ 已知救不回来的一类人：**已经丢了设备 token（清过缓存/换过浏览器）且从来没注册账号**的。
  他们的记录在服务器上，但没有任何凭证能认领。宽墙救不了他们，只能靠人工（提供原始昵称/内容片段核对）。
