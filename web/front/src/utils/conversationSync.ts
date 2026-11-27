// =============================================================================
// 服务端列表同步（conversationSync）
// -----------------------------------------------------------------------------
// P1 的"网络 + 纯函数"层：只做三件事 ——
//   1. 拉 `GET /v1/characters` / `GET /v1/conversations`；
//   2. 把「角色 + 会话」拼成界面认识的卡树（`Card[]`，见 types/chat.ts）；
//   3. 拉某条会话的消息 `GET /v1/conversations/{id}/messages`。
//
// 为什么单独一个文件：
//   - 它是**纯函数 + fetch**，不依赖 Vue / Pinia，可以被 scripts/list-e2e.mjs
//     直接 import 来测（不需要开浏览器）；
//   - 现有的渲染管线（useChatRows / ChatArea）只认 `Conversation.messages`，
//     所以这里只要把服务端数据翻译成同样的结构，界面一行都不用改。
//
// 约定（与 web/back 的 schemas 对齐）：
//   - 角色：{ id(slug), name, avatar(暂时是空串), attribute, active_weight }
//   - 会话：{ id, character_id, mode: 'single'|'group', title, members, next_seq,
//            created_at, last_active_at }
//   - 消息：{ seq, side: 'user'|'assistant'|'system', text, speaker, attachment_url }
//     ⚠ side 是 user/assistant，界面要的是 mine/other，见 toChatMessage()。
//
// 2026-11-18 P1：会话 id 直接挂在子卡上（remoteId），从此不再靠
// `baker_v1_bindings`（角色→会话）猜"这是哪一段"。多段存档共用上下文的
// 老问题（docs/05 §2）也随之消失。
// =============================================================================

import { apiFetch, readError } from './apiClient'
import { v1Url } from '../constants/apiRoutes'
import type { Card, ChatMessage, Conversation } from '../types/chat'

/**
 * 群聊标题（与 chat.ts 的 groupTitle() 同一条规则）
 *
 * 为什么这里再写一遍：本文件要能被 scripts/list-e2e.mjs 用 Node 直接 import
 * （纯函数 + fetch，不碰浏览器资源）。而 `stores/chat.ts` 里链式 import 了
 * `.webp` 头像/材质，Node 版 esbuild 加载不了图片，整个模块就成不了 bundle。
 *
 * 规则保持一致：≤3 个名字用「、」+「和」，更多人用「A、B和C等N人的群聊」。
 * 服务端给了 title 时优先用服务端的（将来支持自定义群名），这里只是兜底。
 */
function groupTitleFor(members: string[]): string {
  if (members.length === 0) return '群聊'
  if (members.length === 1) return `${members[0]}的群聊`
  const head = 3
  const joinWithAnd = (list: string[]): string => {
    if (list.length <= 1) return list[0] ?? ''
    if (list.length === 2) return `${list[0]}和${list[1]}`
    return `${list.slice(0, -1).join('、')}和${list[list.length - 1]}`
  }
  if (members.length <= head) return `${joinWithAnd(members)}的群聊`
  return `${joinWithAnd(members.slice(0, head))}等${members.length}人的群聊`
}

/** 服务端角色（只取用到的字段） */
export interface ServerCharacter {
  id: string
  name: string
  avatar?: string
  attribute?: string
  active_weight?: number
}

/** 服务端会话（只取用到的字段） */
export interface ServerConversation {
  id: string
  character_id?: string | null
  mode: 'single' | 'group' | string
  title?: string
  members?: string[]
  next_seq?: number
  created_at?: string
  last_active_at?: string
}

/** 服务端消息 */
export interface ServerMessage {
  seq: number
  side: 'user' | 'assistant' | 'system' | string
  text?: string
  speaker?: string | null
  attachment_url?: string | null
  created_at?: string
}

/** 列表缓存（断网时拿它画上次的样子） */
export interface ServerListCache {
  at: number
  characters: ServerCharacter[]
  conversations: ServerConversation[]
}

/** 一条会话要拉多少条消息做首屏（再多要等后端支持 before_seq，见 docs/07 §6） */
export const MESSAGE_TAIL = 50

/** 请求失败时抛人话错误（UI 直接显示） */
async function requestJson<T>(path: string): Promise<T> {
  const response = await apiFetch(path, { method: 'GET' })
  if (!response.ok) throw await readError(response)
  return (await response.json()) as T
}

/** 拉角色目录（36 个左右，很小） */
export async function fetchServerCharacters(): Promise<ServerCharacter[]> {
  const data = await requestJson<{ items?: ServerCharacter[] }>('/characters')
  return (data.items ?? []).filter((c) => !!c?.id)
}

/** 拉会话列表（服务端已按 last_active_at 倒序，前端不再自己排） */
export async function fetchServerConversations(): Promise<ServerConversation[]> {
  const data = await requestJson<{ items?: ServerConversation[] }>('/conversations')
  return (data.items ?? []).filter((c) => !!c?.id)
}

/**
 * 拉一条会话的首屏消息
 *
 * `after_seq = max(0, next_seq - 1 - limit)`：seq 从 1 开始，next_seq-1 才是
 * 最后一条，所以"最近 N 条"的起点这样算。⚠ 依赖 seq 连续；将来支持删消息
 * 出现空洞时，后端要加 `tail=1` 或 `before_seq`（docs/07 §6 已记）。
 */
export async function fetchConversationMessages(
  conversationId: string,
  nextSeq = 0,
  limit = MESSAGE_TAIL,
): Promise<{ messages: ServerMessage[]; nextAfterSeq: number }> {
  const tail = Math.max(1, Math.min(500, Math.trunc(limit)))
  const last = Math.max(0, Math.trunc(nextSeq) - 1)
  const afterSeq = Math.max(0, last - tail)
  const data = await requestJson<{ items?: ServerMessage[]; next_after_seq?: number }>(
    `/conversations/${encodeURIComponent(conversationId)}/messages`
      + `?after_seq=${afterSeq}&limit=${tail}`,
  )
  const messages = (data.items ?? []).filter((m) => typeof m?.seq === 'number')
  return {
    messages,
    nextAfterSeq: typeof data.next_after_seq === 'number' ? data.next_after_seq : last,
  }
}

/**
 * 服务端消息 → 界面消息
 *
 * - `side`：user → mine（我），assistant/system → other（她/群里某位）
 * - `id`：直接用 `seq`。seq 是会话内单调递增的，天然当 v-for key 用；
 *   本地临时消息用的是 `nextMessageId()`（当前最大 id + 1），两者不会撞
 *   （从服务端拉回来的消息总是从 1 开始，而本地消息 id 也按 1 起步 —— 这里
 *   只在"整段替换"时用本函数，不会和本地消息混在一起）。
 * - 图片：P1 先按 `[图片]` 占位（与历史导入同口径），真正取图放 P2。
 */
export function toChatMessage(m: ServerMessage): ChatMessage {
  const mine = m.side === 'user'
  const text = (m.text ?? '').trim()
  const msg: ChatMessage = {
    id: Math.max(1, Math.trunc(m.seq)),
    side: mine ? 'mine' : 'other',
    text: m.attachment_url ? (text || '[图片]') : text,
  }
  if (!mine && m.speaker) msg.speakerName = m.speaker
  return msg
}

/** 服务端会话 → 子卡（一段对话） */
function toConversation(
  conv: ServerConversation,
  fallbackName: string,
): Conversation {
  const title = (conv.title ?? '').trim()
  return {
    name: title || fallbackName,
    messages: [],
    remoteId: conv.id,
    remoteCharacterId: conv.character_id ?? undefined,
    remoteTitle: title || undefined,
    remoteTime: conv.last_active_at ?? conv.created_at ?? undefined,
    remoteNextSeq: typeof conv.next_seq === 'number' ? conv.next_seq : 0,
    remoteLoaded: false,
  }
}

/**
 * 「角色 + 会话」→ 卡树
 *
 * 规则（docs/07 §2.4）：
 *   - 一个角色一张单聊卡；它有 N 条服务端会话 → 卡下 N 张子卡（按服务端顺序）
 *   - 没有会话的角色 → 保留一张**占位卡**（子卡无 remoteId，点进去发第一句才建会话）
 *   - `mode='group'` 的会话 → 一张群聊卡（成员来自 members，会话 id 挂在卡上）
 *   - 服务端有、本地角色表里没有的角色 → 照常出卡，头像走默认占位
 *   - 顺序：先按服务端会话出现的顺序（已按 last_active_at 倒序），
 *     再把"从没聊过的角色"按角色表顺序接在后面
 *
 * 纯函数：给同样的输入永远得到同样的卡树（list-e2e 直接测它）。
 */
export function buildServerCards(
  conversations: ServerConversation[],
  characters: ServerCharacter[],
): Card[] {
  const nameById = new Map<string, string>()
  for (const c of characters) nameById.set(c.id, c.name)

  // 角色 id → 该角色的单聊会话（服务端顺序）
  const singleByCharacter = new Map<string, ServerConversation[]>()
  const groupConvs: ServerConversation[] = []
  for (const conv of conversations) {
    if (conv.mode === 'group') {
      groupConvs.push(conv)
      continue
    }
    const characterId = conv.character_id ?? ''
    if (!characterId) continue
    const list = singleByCharacter.get(characterId) ?? []
    list.push(conv)
    singleByCharacter.set(characterId, list)
  }

  const cards: Card[] = []

  // ---- 1) 有会话的角色（按会话顺序），一个角色一张卡 ----
  const characterOrder: string[] = []
  for (const conv of conversations) {
    const cid = conv.character_id ?? ''
    if (conv.mode === 'group' || !cid) continue
    if (!characterOrder.includes(cid)) characterOrder.push(cid)
  }
  for (const cid of characterOrder) {
    const convs = singleByCharacter.get(cid) ?? []
    const name = nameById.get(cid) ?? convs[0]?.title ?? cid
    cards.push({
      conversations: convs.map((conv) => toConversation(conv, name)),
    })
  }

  // ---- 2) 群聊卡（一条 group 会话一张卡）----
  for (const conv of groupConvs) {
    const members = (conv.members ?? []).filter((m) => !!m)
    // 成员不足 2 个的"群"画不成群聊卡（界面按成员数判断类型），
    // 当成一张普通卡显示，避免它凭空消失
    if (members.length < 2) {
      const title = (conv.title ?? '').trim() || '群聊'
      cards.push({
        conversations: [{ ...toConversation(conv, title), name: title }],
      })
      continue
    }
    cards.push({
      members,
      myRole: 'admin',
      speakMode: 'smart',
      groupSessionId: conv.id,
      remoteId: conv.id,
      conversations: [
        {
          ...toConversation(conv, groupTitleFor(members)),
          // 群聊子卡名用群名（列表标题显示「A、B和C的群聊」由 groupTitle 统一）
          name: (conv.title ?? '').trim() || groupTitleFor(members),
        },
      ],
    })
  }

  // ---- 3) 从没聊过的角色：占位卡（发第一句才建会话）----
  for (const c of characters) {
    if (singleByCharacter.has(c.id)) continue
    cards.push({
      conversations: [{ name: c.name, messages: [] }],
    })
  }

  return cards
}

// ---------------------------------------------------------------------------
// 本地优先（2026-11-20）：云端列表**合并**进本机，而不是替换
// ---------------------------------------------------------------------------
// 背景：P1 那版是"服务端列表整棵替换本地卡树"，所以 useChatPersistence 在
// server 模式下**一个字节都不敢写本地**（否则会覆盖掉用户的旧存档）。
// 结果是 server 模式的人本地零副本 —— 线上那批"我记录怎么没了"就是从这里来的。
//
// 现在反过来：本地永远有一份完整副本（写本地不受数据源影响），云端只做
// "补齐本机没有的"。于是即使云端挂了 / 换了域名 / 清了缓存，本地还在。
//
// 合并规则（保守，只加不改）：
//   1. 卡以「首个子卡的名字」为准（单聊=角色名；群聊=群名/成员组合）配对；
//   2. 同一张卡下，子卡先按 remoteId 配对；
//   3. 没有 remoteId 的老子卡，用「消息条数 == 服务端 next_seq - 1」认领
//      （同一条存档在服务端和本地，条数天然一致；认领只补元信息、不覆盖消息）；
//   4. 配不上的服务端子卡/整张卡 → **追加**（消息留空，点进去按需拉）；
//   5. 本地独有的东西一条都不删。
//
// 纯函数：不碰网络、不碰 Vue，`scripts/list-e2e.mjs` 直接测它。
// ---------------------------------------------------------------------------

/** 卡的身份键：单聊用首个子卡的名字，群聊用成员集合（排序后拼起来） */
export function cardIdentity(card: Card): string {
  const members = (card.members ?? []).filter((m) => !!m)
  if (members.length >= 2) return `g:${[...members].sort().join('\u0001')}`
  return `s:${card.conversations[0]?.name ?? ''}`
}

/** 深拷贝一张卡（合并只产出新对象，绝不改调用方传进来的树） */
function cloneCard(card: Card): Card {
  return JSON.parse(JSON.stringify(card)) as Card
}

/**
 * 服务端卡树合并进本地卡树
 *
 * @returns 新数组（本地在前，云端独有的追加在后）；同一条都不重
 */
export function mergeServerCards(local: Card[], server: Card[]): Card[] {
  const out: Card[] = local.map(cloneCard)
  const byIdentity = new Map<string, Card>()
  for (const card of out) {
    const key = cardIdentity(card)
    if (!byIdentity.has(key)) byIdentity.set(key, card)
  }

  for (const incoming of server) {
    const key = cardIdentity(incoming)
    const target = byIdentity.get(key)
    if (!target) {
      const copy = cloneCard(incoming)
      out.push(copy)
      byIdentity.set(key, copy)
      continue
    }

    // 群聊：成员/会话 id 只补空（本地已经有的以本地为准，不动玩家的设置）
    if (incoming.members?.length && !(target.members?.length ?? 0)) {
      target.members = [...incoming.members]
    }
    if (incoming.groupSessionId && !target.groupSessionId) {
      target.groupSessionId = incoming.groupSessionId
    }
    if (incoming.remoteId && !target.remoteId) target.remoteId = incoming.remoteId
    if (incoming.assignTarget && !target.assignTarget) target.assignTarget = incoming.assignTarget

    for (const sConv of incoming.conversations) {
      const sameRemote = sConv.remoteId
        ? target.conversations.find((c) => c.remoteId === sConv.remoteId)
        : undefined
      if (sameRemote) {
        // 同一条会话：只更新服务端元信息，本地消息一条不动
        if (sConv.remoteTitle) sameRemote.remoteTitle = sConv.remoteTitle
        if (sConv.remoteTime) sameRemote.remoteTime = sConv.remoteTime
        if (typeof sConv.remoteNextSeq === 'number') {
          sameRemote.remoteNextSeq = Math.max(
            sameRemote.remoteNextSeq ?? 0,
            sConv.remoteNextSeq,
          )
        }
        continue
      }

      // 老本地存档没有 remoteId：用"条数对得上"认领（见文件头规则 3）
      const claimed = sConv.remoteId
        ? target.conversations.find(
            (c) =>
              !c.remoteId &&
              (c.messages?.length ?? 0) === Math.max(0, (sConv.remoteNextSeq ?? 0) - 1),
          )
        : undefined
      if (claimed) {
        claimed.remoteId = sConv.remoteId
        if (sConv.remoteCharacterId) claimed.remoteCharacterId = sConv.remoteCharacterId
        if (sConv.remoteTitle) claimed.remoteTitle = sConv.remoteTitle
        if (sConv.remoteTime) claimed.remoteTime = sConv.remoteTime
        if (typeof sConv.remoteNextSeq === 'number') claimed.remoteNextSeq = sConv.remoteNextSeq
        // 本地已经有消息了：标成"已拉过"，避免下次点进去把本地消息整段换掉
        claimed.remoteLoaded = true
        continue
      }

      // 服务端给"从没聊过的角色"发的占位对话（没有 remoteId）只对**新卡**有意义
      //（新卡在上面的 `if (!target)` 里整张接过来）。往已有的卡里塞它 = 每次合并
      // 都多一段空对话 —— 实测每合并一次 +36 段（36 个内置角色各一段）。
      if (!sConv.remoteId) continue

      // 本机没有这一段：追加，消息留空，点进去由 ensureMessages 按需拉
      target.conversations.push(JSON.parse(JSON.stringify(sConv)))
    }
  }

  return out
}

/**
 * 头像兜底（服务端 avatar 目前是空串，头像继续用本地资源，见 docs/07 §2.2①）
 *
 * 这里只回传服务端给的值（通常是空串）；界面层的 resolveAvatar()/DEFAULT_AVATAR_URL
 * 会把空串换成默认占位图。本文件刻意不 import 图片资源（Node 单测要能加载）。
 */
export function serverAvatarUrl(char: ServerCharacter | undefined): string {
  return (char?.avatar ?? '').trim()
}

/** 这条消息是不是"远端会话"（有 remoteId 才需要拉历史） */
export function isRemoteConversation(conv: Conversation | undefined | null): boolean {
  return !!conv?.remoteId
}

/**
 * 服务端时间（ISO）→ 列表里那截短时间
 *
 * 例：2026-11-18T01:23:00+08:00 → "11-18 01:23"；
 * 解析不了就返回空串（列表只显示标题，不显示破折号）。
 */
export function formatRemoteTime(iso?: string): string {
  if (!iso) return ''
  const at = Date.parse(iso)
  if (!Number.isFinite(at)) return ''
  const d = new Date(at)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 服务端地址（调试用；与 apiClient 的解析保持一致） */
export function serverBaseUrl(): string {
  return v1Url('')
}
