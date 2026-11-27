# BakerAI 新后端（web/back）

> 只做后端。前端（`web/front`、`cleint`）来适配这套契约，不为前端妥协状态归属。
> 设计文档：[`docs/00-设计定稿-v1.md`](docs/00-设计定稿-v1.md)（先看那份，再看代码）。

## 这一版能干什么

- 设备匿名注册（拿一个 token，之后所有请求都带它）
- 建会话 / 列会话 / 查会话 / 删会话（软删）
- 写消息 / 增量拉消息
- **36 个真实角色**（提示词从老后端原样搬，584 KB 种子）
- **群聊**：三到多人群聊，**四种**发言策略（加权随机 / 轮转 / 主持人判断 / 指定），支持 `END` 停聊、结构化 `@`、本轮无放回
- **世界书检索**：147 条终末地设定入库，三档算法（精确/模糊/语义）逐行移植自聊天终端内核；每轮关键词预取 + 全量索引
- **附件**：图片上传（限 6 MB）、按设备去重、鉴权取回；当前轮图进多模态，历史图不重发
- **老客户端全兼容**：`/chat`、`/chat/stream`、`/chat/suggestions`、`/group/*` 六个端点、`/instances` 全部可用（无 token 也行）
- **干员记忆**：跨会话「人情账」，两种开关、软删防复活、发言权重
- **继续说 / 下一句**：单聊 `POST …/continue`；群聊 `POST …/next` 推进一轮（客户端每 2 秒调一次）
- **输入推荐**：老单聊、老群聊、新客户端三个入口共用一套提示词
- **后台数据接口**：`/v1/admin/*` 十一组端点（概览/用户/角色/配置/密钥/用量/思考链）
- **思考链落库**：`messages.reasoning` 只存服务端，任何下发接口都不带
- **出话**：安全协议 → 世界观 → 角色+风格规则 → 历史 → 检索片段 → 本轮，非流式 + SSE
- **上下文预算**：固定前缀永不裁，历史按预算从最新往回装
- **后处理**：表情 token 翻译、沉浸式括号过滤（流式安全）
- **幂等**：同一个 `client_msg_id` 连发 10 次，库里只有 1 条
- **顺序**：会话内 `seq` 从 1 连续递增，并发写不会撞

还没做（按优先级）：

| 没有 | 说明 |
|---|---|
| 联网搜索 | **故意不做**，检索只走自有世界书；`/v1/meta` 里 `capabilities.web_search=false` |
| 备份定时 | 脚本已就绪（`scripts/backup.py`），定时任务还没挂 |
| 世界书后台编辑 | 现在只能读，改正文要走导入脚本 |
| TTS 语音 | 整条管线未做（服务器有 2×1080Ti，需先实测速度） |
| 流式记账 | SSE 拿不到 usage，暂不记 |

## 环境必须和服务器一致（硬要求）

只在本机开发，但**版本要对齐服务器**——否则会出现"本地好好的，部署时报一个没见过的错"。

| 项 | 服务器 | 本机 |
|---|---|---|
| Python | 3.14.4 | **3.14.5**（同一 minor，patch 差 1） |
| MySQL | 8.4.13（apt） | **8.4.13**（便携版，在 `_runtime/mysql`） |
| Python 依赖 | venv 的 `pip freeze` | **`requirements.lock`**（就是那份 freeze） |

四条规则：

1. **只用 Python 3.14 开发。** `setup_venv.ps1` 会发现 `.venv` 版本不对并自动重建，别手动用别的版本装。
2. **装依赖只走 `requirements.lock`**，不按 `pyproject.toml` 里的范围自己解析——范围只能保证"装得上"。
3. **改依赖的顺序**：服务器上先装好并验证 → 回来更新 lock（命令写在 lock 文件头）。
4. **平台差异单独标注**（例如 `colorama` 只有 Windows 的 pytest 会拖，服务器没有，属正常）。

## 三条命令跑起来

```powershell
# 1. 数据库（便携版 MySQL，程序+数据都在 ..\..\_runtime\mysql）
pwsh -File scripts/dev_mysql.ps1 install     # 只需第一次
pwsh -File scripts/dev_mysql.ps1 start
pwsh -File scripts/dev_mysql.ps1 createdb    # 建 baker_back / baker_back_test

# 2. 虚拟环境（依赖装在 .venv，缓存写 ..\..\_runtime）
pwsh -File scripts/setup_venv.ps1

# 3. 建表 + 起服务
.venv\Scripts\python.exe scripts/migrate.py
.venv\Scripts\uvicorn.exe app.main:app --port 5660
```

打开 <http://127.0.0.1:5660/docs> 可以直接点接口。

## 全部检查

```powershell
pwsh -File scripts/check.ps1          # ruff → mypy → pytest
pwsh -File scripts/check.ps1 -SkipTests   # 只跑静态检查，快
```

## 目录

```
app/
  main.py        装配：中间件、异常出口、探针、路由
  settings.py    配置（环境变量 → 类型化对象）
  db.py          引擎与会话（时区、连接池参数都在这）
  clock.py       时间（只有 UTC 一种写法）
  ids.py         public_id / token / 哈希
  errors.py      唯一错误出口
  schemas.py     请求与响应的形状
  deps.py        鉴权依赖
  migrate.py     执行 schema/*.sql
  models/        13 张表（schema/*.sql 的镜像）
  repos/         所有 SQL
  api/           路由
schema/          建表 SQL（权威，按文件名顺序执行）
scripts/         dev_mysql / setup_venv / migrate / check
tests/           冒烟 + 幂等 + 模型与库一致性
```

## 几条不能破的规矩

1. **所有文件都在 `BakerAI_new` 里**（含 MySQL 数据、pip 缓存、临时文件）。
   `scripts/` 下的脚本已经把 `PIP_CACHE_DIR` / `TMP` / `TEMP` 都改到 `_runtime` 了，别绕过脚本手跑 pip。
2. **SQL 只在 `app/repos/`**；路由层不出现 `select(...)`。
3. **时间只有 `app/clock.py` 的 `utcnow()`**；`grep -rn "datetime.now()" app/` 必须为空。
4. **对外只给 `public_id`**，自增 id 不出服务端。
5. **改表 = 新增 `schema/003_xxx.sql`**，不许回头改 `001_init.sql`（跑过一次的文件不会再跑）。
   改完记得同步 `app/models/`，否则 `tests/test_schema.py` 会挂。
