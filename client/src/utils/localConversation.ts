// =============================================================================
// localConversation.ts —— 每段对话的稳定本地 id
// -----------------------------------------------------------------------------
// 为什么需要它（2026-11-19 修「两段对话互相串」）：
//
//   服务端的一条会话 = 一段对话的上下文。但发送链路以前只按**角色名**记绑定
//   （`baker_v1_bindings` 里 charId → conversationId），于是同一个角色的
//   第 1 段对话和第 2 段对话共用同一条服务端会话：
//   第 1 段里叫了声「亲爱的」，第 2 段她直接无缝接上。
//   前端顶部的注释当时把这个当"已知取舍"记下来了，但对用户来说就是 bug。
//
//   修法：给每段对话一个稳定 id，绑定键变成「角色#这段话的 id」。
//   老数据没有这个字段 → 第一次用到时现生成并写回会话对象，
//   随存档一起持久化（见 utils/zipExport 的 sanitizeConversation 白名单）。
// =============================================================================

import type { Conversation } from '../types/chat'

/**
 * 生成一个新的对话 id
 *
 * 用「时间戳 + 随机」而不是纯随机：排查时能从 id 看出先后，
 * 又不需要引入 uuid 依赖（网页/EXE/APK 三端都能跑）。
 */
export function newLocalId(): string {
  const stamp = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 8)
  return `c-${stamp}-${rand}`
}

/**
 * 拿到这段对话的 id；没有就现生成并写回对象
 *
 * ⚠️ **会修改传入的会话对象**。这是有意的：`conversations[]` 里的对象就是
 * store 的真实状态，写回它才会被持久化；返回一个副本反而会一直重新生成。
 */
export function ensureLocalId(conv: Conversation): string {
  if (conv.localId) return conv.localId
  conv.localId = newLocalId()
  return conv.localId
}

/**
 * 绑定用键：服务端模式下直接用**服务端会话 id**，本地模式下用 `localId`
 *
 * 为什么要分两种：
 *   · 本地存档模式：一段对话就是一个本地对象，`localId` 跟着存档走；
 *   · 服务端列表模式：列表每次刷新都会用新的对象重建（`conversationSync`），
 *     若用 localId 作键，刷新一次就"换了个键"，下一句话会被当成新对话 →
 *     又建一条服务端会话。用服务端 id 作键就天然稳定。
 */
export function conversationKeyOf(conv: Conversation): string {
  if (conv.remoteId) return `r:${conv.remoteId}`
  return ensureLocalId(conv)
}
