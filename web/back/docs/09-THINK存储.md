# THINK（思考过程）存服务端

> 2026-11-17。项目书要求：「思考过程不对用户开放，只作为服务器的存储用，只存在服务器上。」
> 迁移 `schema/016_message_reasoning.sql`，落点 `messages.reasoning`。

## 0. 一句话

模型回了什么，玩家看得见；**模型想了什么，只有服务器知道**。

## 1. 存了什么

| 项 | 说明 |
|---|---|
| 位置 | `messages.reasoning`（MEDIUMTEXT，可空） |
| 一起存的 | 同一条 assistant 消息的 `text` 在另一列，两者互不污染 |
| 什么时候有值 | 只有请求带 `think=true` 时上游才返回思考链；关着就是空 |
| 上游字段名 | 三种都认：`reasoning_content`（最常见）/ `reasoning` / `thinking` |

## 2. 为什么单独一列，不塞进 text

三条都是"塞进去就洗不掉"的后果：

1. **污染历史** —— 下一轮拼提示词时，思考链会被当成人设台词读进去；
2. **污染摘要** —— 压缩时它会被压进「前情提要」，从此永久记着"她想过的念头"；
3. **污染玩家** —— 沉浸式模式本来是让玩家只看台词，思考链一混进来就全破了。

## 3. 不下发（这条是硬的）

| 出口 | 带不带 reasoning |
|---|---|
| `GET /v1/conversations/{id}/messages` | ❌ 不带 |
| `POST /v1/conversations/{id}/messages`（响应） | ❌ 不带 |
| `messages:stream` 的 SSE 帧 | ❌ 不带（流式也不累加思考链） |
| 拼提示词的历史 | ❌ 只用 `text` |

测试 `tests/test_reasoning.py` 专门守着这件事：把响应体整段文本拿去搜思考链，搜不到才算过。

## 4. 现在能拿它干什么（以及还不能）

**能**：直接查库。排障时最有用的两个场景 ——

```sql
-- ① 这句回复为什么跑偏？看她当时"想"了什么
SELECT seq, speaker, LEFT(text, 120) AS reply, LEFT(reasoning, 400) AS think
FROM messages WHERE conversation_id = ? AND reasoning IS NOT NULL ORDER BY seq DESC LIMIT 5;

-- ② 有没有"想的和说的不一致"（人设漏气）
SELECT seq FROM messages WHERE reasoning IS NOT NULL AND reasoning LIKE '%作为AI%';
```

**还不能**：没有接口。要真当成"追踪工具"用，还差两块（都还没做）：

1. **读取口子**：后台/排障用的 `GET /v1/messages/{seq}/reasoning`。
   它必须**只对管理员开**，不能给普通客户端 —— 想链里有大量上下文复述，等于半个记忆泄露。
   而"管理员鉴权"属于后台那一刀（P1），所以这件事跟后台一起做最省事。
2. **保留策略**：思考链随消息无限增长。目前数据量小（每条几百字，1 万条也就几 MB），
   先全留；等真大了再定"保留 N 天"或"只留出错的那几条"。

## 5. 隐私边界（这条要一直记着）

思考链会**复述上下文**：玩家的原话、干员的人设、前情提要里的东西，都可能在里面出现。
所以它和 `messages.text` 是同等级的敏感数据：

- 备份包里会带上它（`db.sql.gz` 全库导出）—— 备份文件要按机密文件对待；
- 将来做"数据导出给用户"时，要不要带上它需要单独决定（我倾向**不带**）；
- 不要为了排查随手导出整表发给别人。

## 6. 流式为什么没存

`stream_text` 是逐段转发的生成器，中途没有"返回值"可以带思考链出来。
要存就得在里面累加一份再回传，改动面比收益大；等后台需要按流式排障时再补。

（上游在流式下确实会先吐 `delta.reasoning_content` 再吐正文 —— 现在的解析**已经忽略它**，
所以至少不会混进正文，只是没存。）
