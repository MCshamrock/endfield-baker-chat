// =============================================================================
// 服务器端「清空数据」—— utils/dataClear.ts
// -----------------------------------------------------------------------------
// 背景（2026-11-18）：
//   设置里「危险操作」那四个按钮原来**只清本机**（Pinia + IndexedDB）。
//   用户实测：「这几个好像不管服务器的数据啊」—— 换台设备一登录，
//   会话、消息、她的记忆全回来了，因为它们一直在服务器上。
//
// 现在四个按钮 = 先清服务器（POST /v1/data/clear），成功了再清本机。
// 顺序不能反：反了就是"本机已经清空、服务器还在"，用户看到一排绿字却发现
// 换个设备数据又回来了 —— 那比报错更糟。
//
// 契约（后端 app/api/data.py，改形状要两边一起改）：
//   POST /v1/data/clear   {conversations, messages, context}
//   → {deleted: {conversations, messages, summaries, memories}}
//   三个 flag 全 false 会 400（前端不会这么发，但接口侧守着）
//
// 归属由设备 token 决定（后端按"登录看账号 / 访客看设备"划界），
// 所以这里**不需要传任何 id** —— 多传 id 反而是越权口子。
// =============================================================================

import { apiJson } from './apiClient'

/** 要清哪几类。和后端字段一一对应，别自己改名。 */
export interface DataClearFlags {
  /** 删全部会话（连带消息/摘要/群成员） */
  conversations: boolean
  /** 删全部消息，会话留着（后端会把 next_seq 归零） */
  messages: boolean
  /** 删摘要 + 干员记忆，并清掉会话上的摘要/记忆副本 */
  context: boolean
}

/** 各表实际删掉的行数 */
export interface DataClearResult {
  conversations: number
  messages: number
  summaries: number
  memories: number
}

/** 调用后端清数据。失败时抛 ApiError（上层据此说"服务器没清掉"）。 */
export async function clearServerData(flags: DataClearFlags): Promise<DataClearResult> {
  const body = await apiJson<{ deleted?: Partial<DataClearResult> }>('/data/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(flags),
  })
  const deleted = body?.deleted ?? {}
  return {
    conversations: Number(deleted.conversations ?? 0),
    messages: Number(deleted.messages ?? 0),
    summaries: Number(deleted.summaries ?? 0),
    memories: Number(deleted.memories ?? 0),
  }
}

/**
 * 把返回的计数拼成一句人话：「服务器已清（会话 1、消息 2、摘要 1、记忆 3）」
 *
 * 为什么四个数都报：用户点「清空全部上下文」时最想知道的是"她到底忘没忘"，
 * 摘要和记忆是两个不同的东西（摘要=这段对话的压缩；记忆=跨会话的人情账），
 * 只报一个数会让人怀疑另一个没清。
 */
export function describeServerClear(result: DataClearResult): string {
  return (
    '服务器已清（' +
    `会话 ${result.conversations}、消息 ${result.messages}、` +
    `摘要 ${result.summaries}、记忆 ${result.memories}）`
  )
}
