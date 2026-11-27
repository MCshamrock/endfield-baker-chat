// =============================================================================
// groupBackend —— 群聊传输层（新后端 /v1）
// -----------------------------------------------------------------------------
// 旧版群聊是独立服务（:5810 / `api.peilika.beer/group`，`/group/session` 等六个
// 端点），调度状态在服务端内存里、历史每轮由前端全量上传。新后端把这套能力
// 收进了 `/v1`：
//
//   建群          POST /v1/conversations        {mode:'group', members:[slug], speak_mode, player_role}
//   玩家说一句    POST /v1/conversations/{id}/messages           → 一条角色回复
//   让下一位说    POST /v1/conversations/{id}/next               → 一条角色回复 + done/remaining
//   推荐          POST /v1/conversations/{id}/suggestions
//   发言权重      PUT  /v1/characters/{slug}/weight              {active: 1..100}
//
// 四种发言策略（服务端 `app/group.py`）：
//   weighted 按 active 权重加权随机（本轮不重复；被 @ 的人优先）—— 新版主路径
//   round    按成员顺序轮转
//   smart    额外问一次主持人，她可以回 END 停聊
//   assign   由玩家指定这一次谁说话
//
// 一条铁律：**一次请求只产生一条角色发言**。"全员说完"由客户端循环 `/next`
// 直到 `done=true`。这个文件里的 `collect()` 就把这个循环封装在里面，
// 界面层（useGroupChat）仍然拿到"一轮的 replies[]"，一行都不用改。
//
// 与界面的约定（签名保持与旧版完全一致）：
//   createGroupSession / requestGroupReply / requestGroupSuggestions /
//   setGroupTopicRemote / closeGroupSession
// =============================================================================

import { v1Url } from '../constants/apiRoutes'
import { apiFetch, ApiError, readError } from './apiClient'
import { CHARACTER_ID_MAP } from './backend'
import { devInfo, devWarn } from './logger'
// 建群 = 真正建一条会话：本局记忆同样在这里定（之后改不了）
import { readNewChatMemory } from './memoryApi'
// 群聊同样尊重「时间感知」（玩家说一句 / 让下一位说 / 老协议 reply 三处）
import { readTimeAware } from './timeAwareness'

/** 群聊发言模式（与后端 `speak_mode` 同名，weighted 是新版主路径） */
export type GroupSpeakMode = 'round' | 'weighted' | 'smart' | 'assign'

/** 一条群聊历史（前端维护；第一次见这个群时导入服务端） */
export interface GroupHistoryItem {
  side: 'mine' | 'other'
  text: string
  /** 说话人中文名（群聊必须：否则无法分辨谁说过什么） */
  speaker?: string
  /** 图片 dataURL（仅玩家可发；导入时只留 [图片] 占位） */
  image?: string
}

export interface GroupReplyItem {
  speaker: string
  text: string
  need_search: boolean
}

export interface GroupSessionInfo {
  session_id: string
  members: string[]
  my_role: string
  speak_mode: GroupSpeakMode
  assign_target: string
  topic: string
  group_name: string
  /** 新后端没有空闲回收（会话在库里），恒为 0 */
  ttl: number
}

export interface GroupReplyResult {
  replies: GroupReplyItem[]
  suggestions: string[]
  done: boolean
  last_speaker: string
  stop_reason: string
}

export interface CreateGroupSessionInput {
  members: string[]
  myRole: string
  speakMode: GroupSpeakMode
  assignTarget?: string
  topic?: string
  groupName?: string
}

/** 统一 POST 的结果（与旧版同形状，调用方不用改） */
export interface PostResult<T> {
  data: T | null
  error: string
  /** 后端建议的等待秒数（限速时返回；0 表示没给） */
  retryAfter: number
  /** 会话已过期/被删（需静默重建，不该弹错误给玩家） */
  sessionExpired: boolean
}

// ---------------------------------------------------------------------------
// 本地元数据：会话 id → 建群时的设置
// ---------------------------------------------------------------------------

interface GroupMeta {
  id: string
  /** 成员中文名（顺序与建群时一致） */
  names: string[]
  /** 成员 slug */
  slugs: string[]
  myRole: string
  speakMode: GroupSpeakMode
  assignTarget: string
  title: string
  topic: string
  /** 历史是否已经导入过（或确认不需要导入） */
  seeded: boolean
  /** 上一次真正发给服务端的话题（变了才在下一句前带一次） */
  lastTopic?: string
}

const META_KEY = 'baker_v1_groups'
const ALIAS_KEY = 'baker_v1_group_alias'

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw) as T
    return parsed && typeof parsed === 'object' ? parsed : fallback
  } catch {
    return fallback
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // 静默失败：本次会话仍能跑，刷新后重新绑定
  }
}

function readMeta(id: string): GroupMeta | undefined {
  return readJson<Record<string, GroupMeta>>(META_KEY, {})[id]
}

function saveMeta(meta: GroupMeta): void {
  const map = readJson<Record<string, GroupMeta>>(META_KEY, {})
  map[meta.id] = meta
  writeJson(META_KEY, map)
}

function removeMeta(id: string): void {
  const map = readJson<Record<string, GroupMeta>>(META_KEY, {})
  if (map[id]) {
    delete map[id]
    writeJson(META_KEY, map)
  }
}

/** 旧 id → 新 id（导入历史会新建一条会话，界面手里的旧 id 需要跟着转） */
function readAlias(): Record<string, string> {
  return readJson<Record<string, string>>(ALIAS_KEY, {})
}

function setAlias(from: string, to: string): void {
  const map = readAlias()
  map[from] = to
  writeJson(ALIAS_KEY, map)
}

/** 顺着别名表找到当前有效的会话 id */
export function resolveGroupSessionId(id: string): string {
  const map = readAlias()
  let current = id
  for (let i = 0; i < 5 && map[current]; i += 1) current = map[current]
  return current
}

// ---------------------------------------------------------------------------
// 服务端形状
// ---------------------------------------------------------------------------

interface ConversationOut {
  id: string
  mode: string
  title: string
  next_seq: number
  speak_mode: string
  members: string[]
  player_role?: string
}

interface MessageCreatedOut {
  seq: number
  duplicate: boolean
  reply: string | null
  reply_seq: number | null
  speaker?: string
  silent?: boolean
}

interface NextTurnOut {
  reply: string | null
  reply_seq: number | null
  speaker: string
  done: boolean
  remaining: string[]
  duplicate?: boolean
}

/** 界面模式 → 后端 speak_mode（目前同名，留一层以防以后改名） */
function toServerMode(mode: GroupSpeakMode): string {
  if (mode === 'weighted' || mode === 'round' || mode === 'smart' || mode === 'assign') {
    return mode
  }
  return 'weighted'
}

/** 后端 speak_mode → 界面模式 */
function toUiMode(mode: string): GroupSpeakMode {
  if (mode === 'round' || mode === 'smart' || mode === 'assign' || mode === 'weighted') {
    return mode
  }
  return 'smart'
}

/** 玩家在群里的显示名（与 useGroupChat.myDisplayName 同口径） */
function selfName(myRole: string): string {
  if (!myRole || myRole === 'admin' || myRole === 'observer') return '管理员'
  return myRole
}

/** 本地时间（只进提示词，不入库） */
function formatLocalTime(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.getDay()]
  // 必须写"时段 + 24 小时制"：只给 "03:15" 时模型经常按 12 小时制理解，
  // 把凌晨三点说成"下午三点"（2026-11-18 实测）。
  const h = now.getHours()
  const period = h < 5 ? '凌晨' : h < 9 ? '早上' : h < 11 ? '上午' : h < 13 ? '中午' : h < 18 ? '下午' : h < 23 ? '晚上' : '深夜'
  return (
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
    `${period}${pad(h)}:${pad(now.getMinutes())}（24小时制） ${week}`
  )
}

/** 本轮临时 id（幂等键）：同一轮内重试复用，跨轮次换新 */
function newClientMsgId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/** ApiError → PostResult 的公共错误映射 */
function failure(error: unknown): PostResult<never> {
  if (error instanceof ApiError) {
    const expired = error.code === 'no_conversation'
    // 界面靠 /限速|429/ 判断要不要退避重试，这里统一成含"限速"的文案
    const message = error.status === 429 ? '上游 AI 限速，请稍后重试' : error.message
    return {
      data: null,
      error: message,
      retryAfter: Math.round(error.retryAfter),
      sessionExpired: expired,
    }
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return { data: null, error: 'aborted', retryAfter: 0, sessionExpired: false }
  }
  return {
    data: null,
    error: error instanceof Error ? error.message : String(error),
    retryAfter: 0,
    sessionExpired: false,
  }
}

// ---------------------------------------------------------------------------
// 建群
// ---------------------------------------------------------------------------

/**
 * 建群
 *
 * 直接建 `/v1` 群聊会话（成员、发言模式、我的身份都在这一刻定下）。
 * 历史不在这里导入：建群那一刻界面还没有历史可给，等第一次 `requestGroupReply`
 * 拿到 `history` 之后再补（见 `seedHistory`）。
 */
export async function createGroupSession(
  input: CreateGroupSessionInput,
  signal?: AbortSignal,
): Promise<{ data: GroupSessionInfo | null; error: string }> {
  const names: string[] = []
  for (const name of input.members) {
    if (name && names.indexOf(name) === -1) names.push(name)
  }
  const slugs = names.map((name) => CHARACTER_ID_MAP[name] || '').filter(Boolean)
  if (slugs.length < 2) {
    return { data: null, error: '群聊至少要有两个已收录的干员' }
  }

  const title = input.groupName?.trim() || names.join('、')
  try {
    const response = await apiFetch('/conversations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        character_id: '',
        mode: 'group',
        title,
        members: slugs,
        speak_mode: toServerMode(input.speakMode),
        player_role: (input.myRole || 'admin').slice(0, 64),
        // 与单聊同一条本地偏好：inherit 跟随全局，on/off 覆盖这一局
        memory: readNewChatMemory(),
      }),
      signal,
    })
    if (!response.ok) throw await readError(response)
    const conversation = (await response.json()) as ConversationOut

    const meta: GroupMeta = {
      id: conversation.id,
      names,
      slugs,
      myRole: input.myRole || 'admin',
      speakMode: input.speakMode,
      assignTarget: input.assignTarget && names.includes(input.assignTarget)
        ? input.assignTarget
        : (names[0] ?? ''),
      title,
      topic: input.topic ?? '',
      seeded: false,
    }
    saveMeta(meta)

    devInfo('[group] 建群', conversation.id, title)
    return {
      data: {
        session_id: conversation.id,
        members: names,
        my_role: meta.myRole,
        speak_mode: input.speakMode,
        assign_target: meta.assignTarget,
        topic: meta.topic,
        group_name: title,
        ttl: 0,
      },
      error: '',
    }
  } catch (err) {
    devWarn('[group] 建群失败:', err)
    return { data: null, error: failure(err).error }
  }
}

// ---------------------------------------------------------------------------
// 历史导入（第一次见这个群时做一次）
// ---------------------------------------------------------------------------

interface ImportMessage {
  side: 'mine' | 'other'
  text: string
  speaker?: string
}

/** 把本地群聊历史拼成老数据包形状（/v1/imports 认这个） */
function buildImportPackage(meta: GroupMeta, history: GroupHistoryItem[]): object | null {
  const messages: ImportMessage[] = []
  for (const item of history) {
    const text = (item.text ?? '').trim()
    if (!text && !item.image) continue
    messages.push({
      side: item.side,
      text: item.image && !text ? '[图片]' : text,
      speaker: item.side === 'mine' ? selfName(meta.myRole) : (item.speaker || ''),
    })
  }
  if (messages.length === 0) return null
  return {
    version: 1,
    myGender: 'male',
    cards: [
      {
        members: meta.names,
        myRole: meta.myRole,
        speakMode: meta.speakMode,
        conversations: [{ name: meta.title, messages }],
      },
    ],
  }
}

/** 导入会新建一条会话：找"刚导进去的那条" */
async function findImportedConversation(meta: GroupMeta): Promise<ConversationOut | null> {
  const response = await apiFetch('/conversations', { method: 'GET' })
  if (!response.ok) return null
  const data = (await response.json()) as { items?: ConversationOut[] }
  const wanted = new Set(meta.names)
  const candidates = (data.items ?? [])
    .filter((item) => item.mode === 'group')
    // 标题不能作为判据：导入时后端会用「成员、成员…」重建标题（不保留自定义群名），
    // 只认成员集合最稳。列表按最近活跃倒序，第一条就是刚导入的那条。
    .filter((item) => {
      const names = item.members ?? []
      return names.length === wanted.size && names.every((name) => wanted.has(name))
    })
  return candidates[0] ?? null
}

/**
 * 把一个群的历史导入服务端（只做一次）
 *
 * 为什么必须导入：服务端只认自己库里的消息。不导入的话，模型对群聊的
 * "前情"一无所知（界面上有记录，她却完全不记得）。
 *
 * 导入成功会得到一条新会话 id —— 用别名表把界面上那个旧 id 指过去，
 * 之后所有请求都自动跟着走（界面不需要知道 id 变了）。
 */
async function seedHistory(
  meta: GroupMeta,
  history: GroupHistoryItem[],
  signal?: AbortSignal,
): Promise<boolean> {
  const pkg = buildImportPackage(meta, history)
  if (!pkg) {
    meta.seeded = true
    saveMeta(meta)
    return false
  }

  const oldId = meta.id
  try {
    const form = new FormData()
    form.append('file', new Blob([JSON.stringify(pkg)], { type: 'application/json' }), 'group.json')
    form.append('mode', 'merge')
    const response = await apiFetch('/imports', { method: 'POST', body: form, signal })
    if (!response.ok) {
      devWarn('[group] 历史导入失败，继续用空会话:', await readError(response))
      meta.seeded = true
      saveMeta(meta)
      return false
    }

    const found = await findImportedConversation(meta)
    if (!found) {
      meta.seeded = true
      saveMeta(meta)
      return false
    }

    // 导入建了新会话：旧的那条（空的）删掉，别名指向新的
    await deleteConversation(oldId)
    removeMeta(oldId)
    setAlias(oldId, found.id)

    meta.id = found.id
    meta.seeded = true
    saveMeta(meta)
    devInfo('[group] 历史已导入', found.id, `${history.length} 条`)
    return true
  } catch (err) {
    devWarn('[group] 历史导入异常:', err)
    meta.seeded = true
    saveMeta(meta)
    return false
  }
}

// ---------------------------------------------------------------------------
// 会话操作
// ---------------------------------------------------------------------------

async function fetchConversation(id: string): Promise<ConversationOut | null> {
  const response = await apiFetch(`/conversations/${encodeURIComponent(id)}`, { method: 'GET' })
  if (response.status === 404) return null
  if (!response.ok) throw await readError(response)
  return (await response.json()) as ConversationOut
}

async function deleteConversation(id: string): Promise<boolean> {
  try {
    const response = await apiFetch(`/conversations/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    })
    return response.ok || response.status === 404
  } catch {
    return false
  }
}

/** 从服务端会话反推元数据（本地 localStorage 被清时用） */
function metaFromConversation(conversation: ConversationOut): GroupMeta {
  const names = conversation.members ?? []
  return {
    id: conversation.id,
    names,
    slugs: names.map((name) => CHARACTER_ID_MAP[name] || '').filter(Boolean),
    myRole: conversation.player_role || 'admin',
    speakMode: toUiMode(conversation.speak_mode),
    assignTarget: names[0] ?? '',
    title: conversation.title,
    topic: '',
    seeded: (conversation.next_seq ?? 0) > 0,
  }
}

/**
 * 「指定发言」要发给后端的标识
 *
 * 【2026-11-20 修 B1】后端按 **slug**（chenqianyu）认人，而本地 `assignTarget`
 * 存的是**中文名**（列表/胶囊上要显示的就是中文名）。以前直接把中文名塞进
 * `assign_speaker` / `speaker`，后端认不出来 → 改由主持人随便挑一个，
 * 用户看到的就是"指定的不是她"。
 *
 * 现在发送前翻译成 slug；翻不出来再退回原名 —— 后端 2026-11-20 起也认名字，
 * 这样老服务端 + 新前端、新服务端 + 老前端 两个方向都不会坏。
 */
function assignSlug(meta: GroupMeta): string {
  if (meta.speakMode !== 'assign') return ''
  const name = meta.assignTarget
  if (!name) return ''
  const idx = meta.names.indexOf(name)
  if (idx >= 0 && meta.slugs[idx]) return meta.slugs[idx]
  return CHARACTER_ID_MAP[name] || name
}

/** 发一条消息并拿一条回复（群聊里服务端只回一条） */
async function postMessage(
  id: string,
  text: string,
  meta: GroupMeta,
  options: { clientMsgId: string; think: boolean; immersive: boolean; mentions?: string[] },
  signal?: AbortSignal,
): Promise<MessageCreatedOut> {
  const response = await apiFetch(`/conversations/${encodeURIComponent(id)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_msg_id: options.clientMsgId,
      text,
      speaker: selfName(meta.myRole),
      assign_speaker: assignSlug(meta),
      mentions: options.mentions ?? [],
      generate: true,
      immersive: options.immersive,
      think: options.think,
      local_time: readTimeAware() ? formatLocalTime() : '',
      time_enabled: readTimeAware(),
    }),
    signal,
  })
  if (!response.ok) throw await readError(response)
  return (await response.json()) as MessageCreatedOut
}

/** 让下一位说一句 */
async function postNext(
  id: string,
  meta: GroupMeta,
  options: { clientMsgId: string; think: boolean; speaker?: string },
  signal?: AbortSignal,
): Promise<NextTurnOut> {
  const response = await apiFetch(`/conversations/${encodeURIComponent(id)}/next`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      client_msg_id: options.clientMsgId,
      local_time: readTimeAware() ? formatLocalTime() : '',
      time_enabled: readTimeAware(),
      think: options.think,
      speaker: options.speaker ?? '',
    }),
    signal,
  })
  if (!response.ok) throw await readError(response)
  return (await response.json()) as NextTurnOut
}

// ---------------------------------------------------------------------------
// 主入口：取"这一步"的回复
// ---------------------------------------------------------------------------

/** 一次 pull 的中间状态 */
interface PullState {
  meta: GroupMeta
  replies: GroupReplyItem[]
  /** 第一句是否是刚发完玩家消息拿到的（那种情况没有 done 字段） */
  firstFromMessage: boolean
  done: boolean
  lastSpeaker: string
  stopReason: string
}

/**
 * 取一步回复（并把 round/weighted 的"全员说完"在内部循环掉）
 *
 * @param once 只取一次（「指定发言」按钮）
 */
export async function requestGroupReply(
  sessionId: string,
  history: GroupHistoryItem[],
  opts: {
    topic?: string
    think?: boolean
    immersiveMode?: boolean
    newTurn?: boolean
    wantSuggestions?: boolean
    useNewPrompt?: boolean
    /** @ 提及的角色 slug（最多 8 个；后端把被点名的人优先安排发言） */
    mentions?: string[]
    /** 只取一次（指定发言） */
    once?: boolean
  } = {},
  signal?: AbortSignal,
): Promise<PostResult<GroupReplyResult>> {
  const id = resolveGroupSessionId(sessionId)
  let meta = readMeta(id)

  try {
    let conversation = await fetchConversation(id)
    if (!conversation) {
      return { data: null, error: '会话不存在', retryAfter: 0, sessionExpired: true }
    }
    if (!meta) {
      meta = metaFromConversation(conversation)
      saveMeta(meta)
    }

    // 这一轮玩家有没有"新发言"（新群里第一次发言也算）
    const last = history[history.length - 1]
    const currentIsPlayer = !!opts.newTurn && last?.side === 'mine'

    // 第一次拿到历史：补导入（导完 meta.id 可能变，重取会话）
    //
    // ⚠ 2026-11-18 修（审计 D1）：**不要把"这一句新发言"一起导进去**。
    // 以前把整包历史（含刚打的那句）一起 POST /imports，于是 justImported=true、
    // 直接走 /next —— 而 /next 不带玩家文本、也不带 mentions，新群第一条的 @ 就丢了。
    // 现在只导入"这一句之前"的历史，这一句按正常消息发（text + mentions + 幂等键）。
    const seedItems = currentIsPlayer ? history.slice(0, -1) : history
    let justImported = false
    if (!meta.seeded && seedItems.length > 0) {
      justImported = await seedHistory(meta, seedItems, signal)
      if (justImported) {
        meta = readMeta(meta.id) ?? meta
        conversation = (await fetchConversation(meta.id)) ?? conversation
      }
    }

    // 话题只在"变了"的时候带一次：新后端没有独立的话题字段，
    // 只能把它作为玩家这句话的引导行送进上下文。
    const topic = (opts.topic ?? meta.topic ?? '').trim()
    if (topic && topic !== meta.lastTopic) meta.topic = topic

    // 只要这一轮是玩家发言就发 /messages（带 mentions）。
    // 不再要求 `!justImported`：上面已经把那句话从导入包里排除了，不存在重复。
    const hasNewPlayerMessage = currentIsPlayer

    const state: PullState = {
      meta,
      replies: [],
      firstFromMessage: false,
      done: false,
      lastSpeaker: '',
      stopReason: '',
    }

    if (hasNewPlayerMessage) {
      const header = topic && topic !== meta.lastTopic ? `【当前话题】${topic}\n` : ''
      const text = `${header}${(last.text ?? '').trim() || '[图片]'}`
      const created = await postMessage(meta.id, text, meta, {
        clientMsgId: newClientMsgId('gm'),
        think: opts.think ?? false,
        immersive: opts.immersiveMode ?? true,
        // @ 提及：显式传 slug（正文里也有 @名字，但别依赖后端文本解析）
        mentions: opts.mentions ?? [],
      }, signal)
      meta.lastTopic = topic || meta.lastTopic
      saveMeta(meta)

      if (created.silent) {
        state.done = true
        state.stopReason = 'END'
      } else if (created.reply) {
        state.replies.push({
          speaker: created.speaker || '',
          text: created.reply,
          need_search: false,
        })
        state.lastSpeaker = created.speaker || ''
        state.firstFromMessage = true
      }
    } else {
      const next = await postNext(meta.id, meta, {
        clientMsgId: newClientMsgId('gn'),
        think: opts.think ?? false,
        speaker: assignSlug(meta),
      }, signal)
      applyNext(state, next)
    }

    // round / weighted：一轮里每位各说一次，直到服务端回 done
    const needsFullRound = meta.speakMode === 'round' || meta.speakMode === 'weighted'
    if (needsFullRound && !state.done && !opts.once) {
      await collect(state, opts, signal)
    }

    // 收尾语义：
    //   · round / weighted：collect 之后 done 由服务端给；单点（once）一次就停
    //   · smart：/next 的 done 原样保留（false = 界面继续循环）；
    //            刚从 /messages 拿到一句时没有 done，按"继续"处理（END 时已置 true）
    //   · assign：一次一条，拿到就停
    if (needsFullRound) {
      if (opts.once) state.done = true
    } else if (meta.speakMode === 'assign') {
      state.done = true
    } else if (state.firstFromMessage) {
      state.done = state.stopReason === 'END'
    }

    return {
      data: {
        replies: state.replies,
        // 推荐改为玩家点开面板时按需拉取（每轮都拉会明显加剧限速）
        suggestions: [],
        done: state.done,
        last_speaker: state.lastSpeaker,
        stop_reason: state.stopReason,
      },
      error: '',
      retryAfter: 0,
      sessionExpired: false,
    }
  } catch (err) {
    devWarn('[group] 取回复失败:', err)
    return failure(err)
  }
}

/** 把 /next 的结果并进状态 */
function applyNext(state: PullState, next: NextTurnOut): void {
  state.done = next.done
  if (next.reply) {
    state.replies.push({
      speaker: next.speaker || '',
      text: next.reply,
      need_search: false,
    })
    state.lastSpeaker = next.speaker || state.lastSpeaker
  } else if (next.done) {
    // 池子空了 / 主持人判定 END：不硬凑一句
    state.stopReason = state.stopReason || 'END'
  }
}

/** 继续问下一位，直到 done（有上限，防服务端异常导致死循环） */
async function collect(
  state: PullState,
  opts: { think?: boolean },
  signal?: AbortSignal,
): Promise<void> {
  const MAX_STEPS = 16
  for (let step = 0; step < MAX_STEPS && !state.done; step += 1) {
    if (signal?.aborted) return
    const next = await postNext(state.meta.id, state.meta, {
      clientMsgId: newClientMsgId('gn'),
      think: opts.think ?? false,
    }, signal)
    applyNext(state, next)
  }
}

// ---------------------------------------------------------------------------
// 推荐
// ---------------------------------------------------------------------------

/** 按需拉取输入推荐（玩家点开面板时才调） */
export async function requestGroupSuggestions(
  sessionId: string,
  _history: GroupHistoryItem[],
  _topic = '',
  signal?: AbortSignal,
): Promise<string[]> {
  const id = resolveGroupSessionId(sessionId)
  try {
    const response = await apiFetch(
      `/conversations/${encodeURIComponent(id)}/suggestions`,
      { method: 'POST', signal },
    )
    if (!response.ok) {
      devWarn('[group] 推荐失败:', await readError(response))
      return []
    }
    const data = (await response.json()) as { suggestions?: unknown }
    if (!Array.isArray(data.suggestions)) return []
    return data.suggestions.filter(
      (item): item is string => typeof item === 'string' && item.trim() !== '',
    )
  } catch (err) {
    devWarn('[group] 推荐异常:', err)
    return []
  }
}

// ---------------------------------------------------------------------------
// 话题 / 关群 / 权重
// ---------------------------------------------------------------------------

/**
 * 设置当前话题
 *
 * 新后端没有"话题"字段：这里把话题存进本地元数据，并往会话里补一条
 * `generate=false` 的提示行（不进 AI 生成，只作为后面 `/next` 的上下文）。
 * 下一条玩家消息也会自带一次 `【当前话题】…` 前缀。
 */
export async function setGroupTopicRemote(sessionId: string, topic: string): Promise<boolean> {
  const id = resolveGroupSessionId(sessionId)
  const meta = readMeta(id)
  if (!meta) return false
  meta.topic = topic
  saveMeta(meta)

  try {
    const response = await apiFetch(`/conversations/${encodeURIComponent(id)}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_msg_id: newClientMsgId('topic'),
        text: `【新话题】${topic}`,
        speaker: '',
        generate: false,
        immersive: true,
        think: false,
      }),
    })
    if (!response.ok) devWarn('[group] 话题写入失败:', await readError(response))
  } catch (err) {
    devWarn('[group] 话题写入异常:', err)
  }
  return true
}

/** 关群：软删服务端会话并清掉本地元数据 */
export async function closeGroupSession(sessionId: string): Promise<void> {
  const id = resolveGroupSessionId(sessionId)
  await deleteConversation(id)
  removeMeta(id)
}

// ---------------------------------------------------------------------------
// 发言权重（「加权模式」用）
// ---------------------------------------------------------------------------

/** 服务端角色表里的一项（只取权重相关字段） */
interface CharacterWeightRow {
  id: string
  name: string
  active_weight: number
}

/** 本地缓存：角色名 → 玩家设过的权重（服务端不提供"读覆盖值"的端点） */
const WEIGHTS_KEY = 'baker_group_weights'

/** 读取玩家设过的权重（本地缓存；没有就是空对象） */
export function readLocalWeights(): Record<string, number> {
  return readJson<Record<string, number>>(WEIGHTS_KEY, {})
}

/**
 * 拉服务端角色表，得到每个角色的**默认**权重
 *
 * 注意：`GET /v1/characters` 返回的是角色自带的 `active_weight`，
 * 不含"这个玩家改过多少"（那个只有 PUT 时才回）。所以界面显示时用
 * `readLocalWeights()` 覆盖默认值。
 */
export async function fetchDefaultWeights(): Promise<Record<string, number>> {
  try {
    const response = await apiFetch('/characters', { method: 'GET' })
    if (!response.ok) return {}
    const data = (await response.json()) as { items?: CharacterWeightRow[] }
    const out: Record<string, number> = {}
    for (const item of data.items ?? []) {
      if (item?.name) out[item.name] = item.active_weight ?? 50
    }
    return out
  } catch (err) {
    devWarn('[group] 拉角色权重失败:', err)
    return {}
  }
}

/** 保存某个干员的发言权重（1–100）；返回是否成功 */
export async function setCharacterWeight(name: string, active: number): Promise<boolean> {
  const slug = CHARACTER_ID_MAP[name] || ''
  if (!slug) return false
  const value = Math.max(1, Math.min(100, Math.round(active)))
  try {
    const response = await apiFetch(
      `/characters/${encodeURIComponent(slug)}/weight`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: value }),
      },
    )
    if (!response.ok) {
      devWarn('[group] 权重保存失败:', await readError(response))
      return false
    }
    const cache = readLocalWeights()
    cache[name] = value
    writeJson(WEIGHTS_KEY, cache)
    return true
  } catch (err) {
    devWarn('[group] 权重保存异常:', err)
    return false
  }
}

// =============================================================================
// 传输层抽象（与旧版同形状：useGroupChat 按 apiMode 二选一）
// =============================================================================

export interface GroupTransport {
  createGroupSession: typeof createGroupSession
  requestGroupReply: typeof requestGroupReply
  requestGroupSuggestions: typeof requestGroupSuggestions
  setGroupTopicRemote: typeof setGroupTopicRemote
  closeGroupSession: typeof closeGroupSession
}
