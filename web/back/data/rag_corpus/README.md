# 世界书语料（老后端 RAG 库）

`docs.jsonl` 是**老后端向量库的原样拷贝**，不是我们另写的。

## 它是什么

老网站（原始网站前后端）的 RAG 检索服务 `rag_service.py` 跑在构建机上，
向量索引在 `/run/media/y/游戏/local-llm/rag/index-endfield-v2/`：

| 文件 | 内容 |
|---|---|
| `emb.npy` | 4408 × 768 的向量（bge-endfield-v2 微调模型），本仓库不需要 |
| `docs.jsonl` | **就是本目录这份**：每行一块语料 |
| `meta.json` | 构建信息：`count: 4408`、`dim: 768`、`built_at: 2026-11-08 22:55:14` |

每行字段：

```json
{"id": 0, "doc_id": "世界任务/刀剑无名·其一.md#0", "category": "world",
 "title": "刀剑无名·其一", "text": "【刀剑无名·其一】言：目前可以先记录位置……"}
```

- `doc_id` = 「源文件路径 # 第几块」，547 个文件切成 4408 块（每块 200 字上下）
- `category`：`main` / `side` / `story` / `channel` / `industry` / `world` /
  `material` / `archive` / `event` / `misc`
- `text` 开头那行 `【…】` 是分节标题，导入时会剥掉

## 怎么用

不要手改数据库，也不要手改这份文件。换语料的正路是：

1. 把构建机上**最新的** `index-endfield-v2/docs.jsonl` 拷来覆盖这份；
2. 跑 `python scripts/import_rag_corpus.py --prune`（在 `back/` 目录、用 venv）；
3. 脚本会重新算触发词并覆盖写 `worldbook` 表，多出来的旧条目用 `--prune` 清掉。

## 为什么拷进仓库

- 部署机（构建机）上虽然有原件，但那块盘是数据盘、`/run/media` 一重启就可能没挂上；
- 换语料必须能"回看上一次是什么样"，仓库里有一份才好对比。

体量：3.2 MB / 4408 行 / 547 个文件 / 约 90 万字。

## 补充库（supplement_*.jsonl）

`docs.jsonl`（base）之外还有三份补充，**格式与 base 条目一致**，`import_rag_corpus.py`
会一起导入、`--prune` 也一起保护：

| 文件 | 条数 | 从哪来 |
|---|---|---|
| `supplement_prts.jsonl` | 422（33 人） | 本地 PRTS 语料库的角色档案（`$DSH_HOME/prts-corpus/releases` 里的 `character_profile`）。官方原文，按句切 ~400 字一条，只去掉每行的小节前缀 |
| `supplement_handwritten.jsonl` | 23 | 手写世界书里 RAG 语料一次都没出现的条目（规则/机制为主） |
| `supplement_preview.jsonl` | 18 | 2.0 前瞻公开内容（PRTS 那份是 11-02 的，没有 2.0） |

重新生成：

```bash
python scripts/build_supplement_prts.py              # PRTS 那份（--check 只统计）
python scripts/import_rag_corpus.py --prune          # base + 三份补充一起导入
python scripts/import_rag_corpus.py --no-supplement  # 只导 base（排查用）
```

`supplement_preview.jsonl` 没有生成脚本 —— 里面的正文是照 11-21 前瞻直播整理写的，
出处是 `F:\dshworkspace\video_production\终末地2.0\终末地2.0前瞻_内容总结与切片索引.md`。
PRTS 更新到 2.0 之后，这批可以用 PRTS 原文替换（改完直接覆盖文件，`source` 改成 `prts`）。
