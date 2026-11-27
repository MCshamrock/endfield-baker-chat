// =============================================================================
// legacyLocalCore —— 老版本地数据（IndexedDB / 老客户端 JSON 文件）的纯函数核心
// -----------------------------------------------------------------------------
// 这个文件**故意不 import 任何东西**，原因有两个：
//
//   1. 它要被 `scripts/legacy-migrate-smoke.mjs` 直接 import（Node 22 原生剥类型）。
//      只要模块里有一条运行时 import，Node 就得解析那条路径（TS 源里写的是无扩展名
//      相对路径），脚本会直接起不来。
//   2. 这里的逻辑（合并分片、字段翻译、导入包拼装、上传编排）本来就不该知道
//      DOM / localStorage / IndexedDB 长什么样 —— 那些在 utils/legacyLocalData.ts。
//
// 老数据的三种来源（键名完全一致，只是介质不同）：
//   · 老网页版（v3.2.2）  IndexedDB `endfield-baker` / store `data`
//       cardsV2 = { v:2, slots:[0,1,...], next, keys:['s:伊冯',...] }
//       card:0  = { conversations:[...], members?:[...] }
//       cards   = 旧的整库快照（3.2.2 迁移到分片后**不再更新**，但一直保留）
//   · 老客户端（EXE/APK）  同目录 `endfield-baker-data.json`，键名与 IndexedDB 相同
//   · 老导出包             project.json + cards/NN/conversations/NN-名字.json
//
// 第 1 种是主路径（新前端覆盖部署在同一 origin 上，浏览器的 IndexedDB 还是那一份）；
// 第 2 种只能由打包端读；第 3 种是"读不到就导出再导入"的兜底。
// =============================================================================

/** 老版本 IndexedDB 的库名 / store 名（v3.2.2 与线上 web31 都是这一套） */
export const LEGACY_DB_NAME = 'endfield-baker'
export const LEGACY_STORE_NAME = 'data'

/**
 * 库名前缀
 *
 * 线上 web31（chat.peilika.beer）与 3.2.2（test2.peilika.beer）**库名完全相同**，
 * 都是 `endfield-baker`（已逐文件核对两套源码）。保留这个前缀是为了：
 *   · 将来若某条线改名，读取器能按前缀把同源的兄弟库一起读出来；
 *   · 覆盖部署时用户可能同时留下过测试库，能多读一份就多一条线索（合并不出事）。
 * 真正的隔离来自 **origin**：不同域名的 IndexedDB 互相看不见，所以“两个域名都开过”
 * 时只会读到当前 origin 的这份，另一份不会被动到。
 */
export const LEGACY_DB_NAME_PREFIX = 'endfield-baker'

/** 卡片数据的两个 key：旧整库快照 + 3.2.2 起的分片索引 */
export const LEGACY_CARDS_KEY = 'cards'
export const LEGACY_CARD_INDEX_KEY = 'cardsV2'
/** 分片键前缀：`card:<slot>` */
export const LEGACY_CARD_SLOT_PREFIX = 'card:'

/** 其余持久化键（新前端只读，绝不写） */
export const LEGACY_SETTINGS_KEY = 'settings'
export const LEGACY_MY_GENDER_KEY = 'myGender'
export const LEGACY_VERSION_KEY = 'version'
export const LEGACY_STRIP_VARIANT_KEY = 'stripVariant'
export const LEGACY_USAGE_KEY = 'usage'
export const LEGACY_CARD_ACTIVITY_KEY = 'cardActivity'
export const LEGACY_SUB_ACTIVITY_KEY = 'subActivity'

/** 打包端（Electron / Capacitor）的整库文件（与 IndexedDB 同一套键） */
export const LEGACY_NATIVE_FILE = 'endfield-baker-data.json'

/**
 * 迁移完成标记（localStorage）
 *
 * 只写新 key，绝不改老 key：用户想回滚到老站点时，老站读到的还是原来那份数据。
 * 带 `.v1`：将来若导入规则变了（例如能迁 settings 了），可以升版本重跑一次。
 */
export const MIGRATE_MARKER_KEY = 'bakerai.legacy-migrated.v1'

/** 老 Service Worker 清理标记（同一台浏览器只清一次，别把将来的新缓存也删了） */
export const SW_CLEAN_MARKER_KEY = 'bakerai.legacy-sw-cleaned.v1'

/** 后端单次导入上限（web/back `app/settings.py`：import_max_bytes = 64MB） */
export const IMPORT_MAX_BYTES = 64 * 1024 * 1024

/** 群聊判定门槛（与老客户端 / 新后端一致：成员 ≥2） */
export const GROUP_MIN_MEMBERS = 2

/** 新后端认的群聊发言模式（老版 3 种 + 新前端 weighted） */
export const SPEAK_MODES = ['round', 'smart', 'assign', 'weighted'] as const

// ---------------------------------------------------------------------------
// 类型：只描述"我们真的会读/会写"的字段，别的原样忽略
// ---------------------------------------------------------------------------

/** 老消息的方向只有两种；`role` 是更老的别名，读取时一起认 */
export type LegacySide = 'other' | 'mine'

export interface LegacyMessage {
  /** 对话内序号（老数据一定有；坏数据缺失时按数组位置补） */
  id: number
  side: LegacySide
  text: string
  image?: string
  imageW?: number
  imageH?: number
  /** 群聊「新话题」提示行：屏上显示、不进 AI 上下文 */
  topic?: boolean
  /** 新版「对话总结」卡片：同样不该被当成谁说的话喂给模型 */
  summary?: boolean
  speakerName?: string
  speakerAvatar?: string
  mood?: string
  /** 请求失败的占位气泡：不是聊天记录，迁移时丢掉 */
  isError?: boolean
  regenerateUserId?: number
}

export interface LegacyContextEntry {
  side: LegacySide
  text: string
  image?: string
}

export interface LegacyConversation {
  name: string
  messages: LegacyMessage[]
  contextHistory?: LegacyContextEntry[]
  /**
   * 下面这几个是**线上 web31（3.1）独有**的会话级账本字段（3.2.2 已不再写入）：
   * sessionOutputTokens / promptTokens / lastPromptTokens 是 token 用量，
   * summaryAnchor / compactedBefore 是「对话总结」压缩过的位置。
   * 新后端没有对应列（压缩由服务端自己做），所以只读出来做统计/诊断，**不迁移**。
   */
  sessionOutputTokens?: number
  promptTokens?: number
  lastPromptTokens?: number
  summaryAnchor?: number
  compactedBefore?: number
  suggestions?: { forMessageId: number; items: string[]; at: number }
}

export interface LegacyCard {
  conversations: LegacyConversation[]
  members?: string[]
  myRole?: string
  speakMode?: string
  assignTarget?: string
  groupRunning?: boolean
  groupTopic?: string
}

/** 一次读取得到的"老数据全貌"（已经合并过旧快照 + 分片） */
export interface LegacyLocalData {
  cards: LegacyCard[]
  myGender: 'male' | 'female'
  promptOverrides: Record<string, string>
  /** 这次数据来自哪几处，给界面/日志看（例如 ['cardsV2', 'cards']） */
  sources: string[]
}

/** 检测结果：任务要求的四个字段 + 几个给提示条用的补充数字 */
export interface LegacyDetectSummary {
  /** 有消息（或只有 AI 记忆可兜底）的卡片数 */
  cards: number
  /** 有内容、会被建会话的对话段数 */
  conversations: number
  /** 迁移后会变成消息的条数（屏上消息优先，屏上为空时用 AI 记忆兜底） */
  messages: number
  hasData: boolean
  /** 会被丢弃的失败气泡 / 空消息条数 */
  skipped: number
  /** 以 AI 记忆兜底进消息的条数（这些段的老"屏上消息"是空的） */
  contextFallback: number
  /** 自定义角色提示词条数（会写进 device_prompts） */
  promptOverrides: number
  myGender: 'male' | 'female'
  /** 是否发现迁移完成标记 */
  migrated: boolean
}

// ---------------------------------------------------------------------------
// 小工具：把 unknown 收成可用形状（老库里什么都可能有，绝不因一条坏数据整库失败）
// ---------------------------------------------------------------------------

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function firstArray(rec: Record<string, unknown>, keys: string[]): unknown[] {
  for (const key of keys) {
    const value = rec[key]
    if (Array.isArray(value)) return value
  }
  return []
}

/** 群聊字段可能平铺在卡片上，也可能塞在 group / groupInfo / groupMeta / groupConfig 里 */
function flattenGroupScope(rec: Record<string, unknown>): Record<string, unknown> {
  for (const key of ['group', 'groupInfo', 'groupMeta', 'groupConfig']) {
    const nested = rec[key]
    if (isRecord(nested)) return { ...rec, ...nested }
  }
  return rec
}

/** 成员名单：去空、去重、保序；一个都没有 → 空数组（= 单聊卡） */
function pickMembers(scope: Record<string, unknown>): string[] {
  for (const key of ['members', 'groupMembers', 'group_members', 'memberNames', 'memberList']) {
    const value = scope[key]
    if (!Array.isArray(value)) continue
    const names: string[] = []
    for (const item of value) {
      if (typeof item !== 'string') continue
      const name = item.trim()
      if (name && !names.includes(name)) names.push(name)
    }
    if (names.length > 0) return names
  }
  return []
}

// ---------------------------------------------------------------------------
// 归一化：老库里的卡片 → LegacyCard（只保留认识的字段，其余不动）
// ---------------------------------------------------------------------------

/** 方向归一：优先 `side`（mine/other），其次 `role`（user/assistant/system） */
function sideOf(raw: Record<string, unknown>): LegacySide {
  if (raw.side === 'mine') return 'mine'
  if (raw.side === 'other') return 'other'
  if (raw.role === 'user') return 'mine'
  return 'other'
}

function normalizeMessage(raw: unknown, index: number): LegacyMessage | null {
  if (!isRecord(raw)) return null
  const side = sideOf(raw)
  const msg: LegacyMessage = {
    // 老数据一定是数字 id；缺了就用数组位置补，保证前端 v-for / 合并去重还有键可用
    id: finiteNumber(raw.id) ?? index + 1,
    side,
    text: str(raw.text) || str(raw.content),
  }
  const image = str(raw.image)
  if (image) msg.image = image
  const w = finiteNumber(raw.imageW)
  const h = finiteNumber(raw.imageH)
  if (w !== undefined) msg.imageW = w
  if (h !== undefined) msg.imageH = h
  // topic 也可能由 role:'system' 表达（更老的包）；两种都算"提示行"
  if (raw.topic === true || raw.role === 'system') msg.topic = true
  if (raw.summary === true) msg.summary = true
  const speaker = str(raw.speakerName) || str(raw.speaker)
  if (speaker) msg.speakerName = speaker
  const avatar = str(raw.speakerAvatar)
  if (avatar) msg.speakerAvatar = avatar
  const mood = str(raw.mood)
  if (mood) msg.mood = mood
  if (raw.isError === true) msg.isError = true
  const regen = finiteNumber(raw.regenerateUserId)
  if (regen !== undefined) msg.regenerateUserId = regen
  return msg
}

function normalizeContextEntry(raw: unknown): LegacyContextEntry | null {
  if (!isRecord(raw)) return null
  const text = str(raw.text) || str(raw.content)
  // 新后端的 AI 记忆兜底只认"有文字"的条目（纯图条目会被它跳过），这里保持一致，
  // 免得界面上报"兜底 N 条"而后端实际只进了 N-1 条。
  if (!text.trim()) return null
  const entry: LegacyContextEntry = { side: sideOf(raw), text }
  const image = str(raw.image)
  if (image) entry.image = image
  return entry
}

function normalizeConversation(raw: unknown): LegacyConversation | null {
  if (!isRecord(raw)) return null
  const messagesRaw = firstArray(raw, ['messages', 'items'])
  const messages: LegacyMessage[] = []
  messagesRaw.forEach((item, index) => {
    const msg = normalizeMessage(item, index)
    if (msg) messages.push(msg)
  })
  const contextRaw = raw.contextHistory
  let contextHistory: LegacyContextEntry[] | undefined
  if (Array.isArray(contextRaw)) {
    contextHistory = []
    for (const item of contextRaw) {
      const entry = normalizeContextEntry(item)
      if (entry) contextHistory.push(entry)
    }
  }
  const conv: LegacyConversation = {
    name: str(raw.name) || str(raw.title),
    messages,
  }
  if (contextHistory) conv.contextHistory = contextHistory
  // 3.1 的 token / 总结账本字段：读出来但不进导入包（新后端自己算），
  // 保留是为了将来排查"为什么压缩时机不一样"时有据可查。
  const outputTokens = finiteNumber(raw.sessionOutputTokens)
  if (outputTokens !== undefined) conv.sessionOutputTokens = outputTokens
  const promptTokens = finiteNumber(raw.promptTokens)
  if (promptTokens !== undefined) conv.promptTokens = promptTokens
  const lastPromptTokens = finiteNumber(raw.lastPromptTokens)
  if (lastPromptTokens !== undefined) conv.lastPromptTokens = lastPromptTokens
  const summaryAnchor = finiteNumber(raw.summaryAnchor)
  if (summaryAnchor !== undefined) conv.summaryAnchor = summaryAnchor
  const compactedBefore = finiteNumber(raw.compactedBefore)
  if (compactedBefore !== undefined) conv.compactedBefore = compactedBefore
  return conv
}

export function normalizeLegacyCard(raw: unknown): LegacyCard | null {
  if (!isRecord(raw)) return null
  const conversationsRaw = firstArray(raw, ['conversations', 'subs', 'children'])
  if (conversationsRaw.length === 0) return null
  const conversations: LegacyConversation[] = []
  for (const item of conversationsRaw) {
    const conv = normalizeConversation(item)
    if (conv) conversations.push(conv)
  }
  if (conversations.length === 0) return null

  const scope = flattenGroupScope(raw)
  const card: LegacyCard = { conversations }
  const members = pickMembers(scope)
  if (members.length >= GROUP_MIN_MEMBERS) card.members = members
  const myRole = str(scope.myRole) || str(scope.role)
  if (myRole) card.myRole = myRole
  const speakMode = (str(scope.speakMode) || str(scope.mode)).toLowerCase()
  if ((SPEAK_MODES as readonly string[]).includes(speakMode)) card.speakMode = speakMode
  const assignTarget = str(scope.assignTarget) || str(scope.target)
  if (assignTarget) card.assignTarget = assignTarget
  if (typeof scope.groupRunning === 'boolean') card.groupRunning = scope.groupRunning
  if (typeof scope.running === 'boolean') card.groupRunning = scope.running
  const groupTopic = str(scope.groupTopic) || str(scope.topic)
  if (groupTopic) card.groupTopic = groupTopic
  return card
}

/** 整库值 → 卡片数组；不是数组 / 元素坏掉的一律跳过（一条坏卡不能毁掉整库） */
export function normalizeLegacyCards(raw: unknown): LegacyCard[] {
  if (!Array.isArray(raw)) return []
  const cards: LegacyCard[] = []
  for (const item of raw) {
    const card = normalizeLegacyCard(item)
    if (card) cards.push(card)
  }
  return cards
}

// ---------------------------------------------------------------------------
// 合并：3.2.2 的分片 + 旧整库快照
// ---------------------------------------------------------------------------
// 为什么必须合并：
//   3.2.2 把卡片改成分片存储（cardsV2 + card:N）之后，`cards` 这个旧整库键
//   **只作为回退快照保留、不再更新**。升级到 3.2.2 之后聊的消息只在分片里；
//   而新前端基座（3.1 线）只读 `cards` —— 直接读会看到"升级那一刻的旧快照"，
//   后面的聊天记录看起来像丢了。合并两处、按消息 id 去重，才叫"完美读到"。
//
// 反过来也要防：新前端在自己的 `cards` 键里继续写新消息，分片里没有它们。
// 所以不是"分片优先"，而是**两边的并集**。

/** 卡片身份：群聊按成员集合，单聊按第一个子对话的角色名（与老 cardActiveKey 同思路） */
export function cardIdentity(card: LegacyCard): string {
  const members = card.members ?? []
  if (members.length >= GROUP_MIN_MEMBERS) {
    return 'g:' + members.slice().sort().join('|')
  }
  return 's:' + (card.conversations[0]?.name ?? '')
}

/** 一条消息的内容指纹（比较时不把整张 dataURL 拼进去，几十 MB 的库扛不住） */
function messageSignature(msg: LegacyMessage): string {
  const image = msg.image ?? ''
  // 图片取"长度 + 头 64 + 尾 64"：同一张图必然相同，不同图几乎不可能撞
  const imageTag = image ? `${image.length}:${image.slice(0, 64)}:${image.slice(-64)}` : ''
  return [
    msg.side,
    msg.topic ? 't' : '',
    msg.summary ? 's' : '',
    msg.speakerName ?? '',
    msg.mood ?? '',
    msg.text,
    imageTag,
  ].join('\u0000')
}

function mergeMessages(
  base: LegacyMessage[],
  extra: LegacyMessage[],
): LegacyMessage[] {
  if (extra.length === 0) return base
  const out = base.slice()
  const seenContent = new Set(out.map(messageSignature))
  const usedIds = new Set(out.map((msg) => msg.id))
  let maxId = 0
  for (const msg of out) if (msg.id > maxId) maxId = msg.id

  for (const msg of extra) {
    const signature = messageSignature(msg)
    // 同一条消息在两处各存一份是常态（升级前后的快照），按内容去重
    if (seenContent.has(signature)) continue
    let id = msg.id
    if (usedIds.has(id)) {
      // id 撞了但内容不同：两边是"不同时间线的同一序号"（例如删过存档后下标错位）。
      // 不能覆盖任何一条 —— 给后来者发一个新 id 挂到末尾，宁可顺序略乱也不丢内容。
      maxId += 1
      id = maxId
    }
    const copy: LegacyMessage = { ...msg, id }
    out.push(copy)
    usedIds.add(id)
    seenContent.add(signature)
  }
  return out
}

function mergeContext(
  base: LegacyContextEntry[] | undefined,
  extra: LegacyContextEntry[] | undefined,
): LegacyContextEntry[] | undefined {
  if (!base && !extra) return undefined
  const out: LegacyContextEntry[] = []
  const seen = new Set<string>()
  const push = (entry: LegacyContextEntry) => {
    const key = [
      entry.side,
      entry.text,
      entry.image ? `${entry.image.length}:${entry.image.slice(0, 32)}` : '',
    ].join('\u0000')
    if (seen.has(key)) return
    seen.add(key)
    out.push(entry)
  }
  for (const entry of base ?? []) push(entry)
  for (const entry of extra ?? []) push(entry)
  return out
}

function mergeConversation(base: LegacyConversation, extra: LegacyConversation): void {
  base.messages = mergeMessages(base.messages, extra.messages)
  const context = mergeContext(base.contextHistory, extra.contextHistory)
  if (context) base.contextHistory = context
  const baseTokens = base.sessionOutputTokens ?? 0
  const extraTokens = extra.sessionOutputTokens ?? 0
  if (extraTokens > baseTokens) base.sessionOutputTokens = extraTokens
  if (!base.suggestions && extra.suggestions) base.suggestions = extra.suggestions
}

/** 两段对话是不是"同一段"（名字相同 + 消息内容有交集或其中一边是空的） */
function sameConversation(a: LegacyConversation, b: LegacyConversation): boolean {
  if (a.name !== b.name) return false
  if (a.messages.length === 0 || b.messages.length === 0) return true
  const signatures = new Set(a.messages.map(messageSignature))
  // 看前 5 条有没有交集，避免把"同一个角色的两段不同存档"错并成一段
  return b.messages.slice(0, 5).some((msg) => signatures.has(messageSignature(msg)))
}

function mergeCardInto(target: LegacyCard, extra: LegacyCard): void {
  // 常见情况：两边长度一致（同一张卡，只是版本新旧）→ 按位置逐段合并
  if (target.conversations.length === extra.conversations.length) {
    for (let i = 0; i < target.conversations.length; i += 1) {
      const a = target.conversations[i]
      const b = extra.conversations[i]
      // 位置相同但名字不同 = 结构已经错位，宁可另起一段也不混两条时间线
      if (a && b && (a.name === b.name || !a.name || !b.name)) mergeConversation(a, b)
      else if (a && b) target.conversations.push(b)
    }
  } else {
    const used = new Set<number>()
    for (const conv of extra.conversations) {
      let matched = false
      for (let i = 0; i < target.conversations.length; i += 1) {
        if (used.has(i)) continue
        if (sameConversation(target.conversations[i], conv)) {
          mergeConversation(target.conversations[i], conv)
          used.add(i)
          matched = true
          break
        }
      }
      // 找不到对应段就**追加**：多一段重复，好过多丢一段
      if (!matched) target.conversations.push(conv)
    }
  }
  if (!target.members && extra.members) target.members = extra.members
  if (!target.myRole && extra.myRole) target.myRole = extra.myRole
  if (!target.speakMode && extra.speakMode) target.speakMode = extra.speakMode
  if (!target.assignTarget && extra.assignTarget) target.assignTarget = extra.assignTarget
  if (target.groupRunning === undefined && extra.groupRunning !== undefined) {
    target.groupRunning = extra.groupRunning
  }
  if (!target.groupTopic && extra.groupTopic) target.groupTopic = extra.groupTopic
}

/**
 * 合并分片卡片与旧整库快照
 *
 * @param shardCards 分片读出来的卡片（cardsV2 的 slots 顺序），没有就传 null
 * @param legacyCards 旧整库 `cards` 键，没有就传 null
 */
export function mergeLegacyCards(
  shardCards: unknown,
  legacyCards: unknown,
): LegacyCard[] {
  const fromShards = normalizeLegacyCards(shardCards)
  const fromLegacy = normalizeLegacyCards(legacyCards)
  if (fromShards.length === 0) return fromLegacy
  if (fromLegacy.length === 0) return fromShards

  const result: LegacyCard[] = []
  const byIdentity = new Map<string, LegacyCard>()
  for (const card of fromShards) {
    const key = cardIdentity(card)
    // 分片内部出现同身份（极少见）时保留成两张，别把两张不同的卡并了
    if (byIdentity.has(key)) {
      result.push(card)
      continue
    }
    byIdentity.set(key, card)
    result.push(card)
  }
  for (const card of fromLegacy) {
    const key = cardIdentity(card)
    const target = byIdentity.get(key)
    if (target) mergeCardInto(target, card)
    else {
      byIdentity.set(key, card)
      result.push(card)
    }
  }
  return result
}

// ---------------------------------------------------------------------------
// 统计：迁移前给用户看的"N 张卡 / M 段对话 / K 条消息"
// ---------------------------------------------------------------------------

/** 一条消息能不能迁：不是错误气泡，且有文字或图片 */
export function isImportableMessage(msg: LegacyMessage): boolean {
  if (msg.isError) return false
  return !!msg.text.trim() || !!msg.image
}

/** 屏上消息优先；屏上一条能用的都没有时才用 AI 记忆兜底（与后端规则一致） */
export function effectiveMessages(conv: LegacyConversation): LegacyMessage[] {
  const screen = conv.messages.filter(isImportableMessage)
  if (screen.length > 0) return screen
  return []
}

export interface LegacyCounts {
  cards: number
  conversations: number
  messages: number
  skipped: number
  contextFallback: number
}

export function countLegacyCards(cards: LegacyCard[]): LegacyCounts {
  let cardCount = 0
  let conversationCount = 0
  let messageCount = 0
  let skipped = 0
  let contextFallback = 0

  for (const card of cards) {
    let cardHasData = false
    for (const conv of card.conversations) {
      const screen = conv.messages.filter(isImportableMessage)
      // 错误气泡和"空消息"都计入 skipped，提示条上要想说得清"丢了几条"
      skipped += conv.messages.length - screen.length
      const context = conv.contextHistory ?? []
      if (screen.length > 0) {
        messageCount += screen.length
        conversationCount += 1
        cardHasData = true
      } else if (context.length > 0) {
        // 屏上清空过、但 AI 记忆还在 → 这段记忆会被兜底成消息
        messageCount += context.length
        contextFallback += context.length
        conversationCount += 1
        cardHasData = true
      }
    }
    if (cardHasData) cardCount += 1
  }
  return {
    cards: cardCount,
    conversations: conversationCount,
    messages: messageCount,
    skipped,
    contextFallback,
  }
}

// ---------------------------------------------------------------------------
// 转换：本地卡片 → 新后端能吃的导入包
// ---------------------------------------------------------------------------
// 原则：**只翻译字段，不替后端做业务判断**。
//   · side(mine/other) → role(user/assistant)；topic/summary → role(system)
//   · isError 丢掉（它本来就不是聊天记录）
//   · contextHistory **原样带上**：屏上消息为空时后端会拿它兜底，屏上有消息时
//     后端会把它计入 dropped_context 并在 notes 里说清 —— 这个判断交给后端，
//     前端不替它决定，报告里的数字才和真实一致。
//   · 图片保持 dataURL 内联（JSON 包形态；新后端 imports/images.py 直接解）。

/** 新后端导入包里的消息形状（字段名以 app/imports/normalize.py 为准） */
export interface ImportMessage {
  role: 'user' | 'assistant' | 'system'
  text: string
  speakerName?: string
  mood?: string
  image?: string
}

export interface ImportContextEntry {
  role: 'user' | 'assistant'
  text: string
  image?: string
}

export interface ImportConversation {
  name: string
  messages: ImportMessage[]
  contextHistory?: ImportContextEntry[]
}

export interface ImportCard {
  conversations: ImportConversation[]
  members?: string[]
  myRole?: string
  speakMode?: string
}

export interface ImportPackage {
  version: 1
  myGender: 'male' | 'female'
  cards: ImportCard[]
  promptOverrides?: Record<string, string>
}

export interface ConversionStats {
  cards: number
  conversations: number
  /** 实际会进库的消息数（含"屏上为空、用 AI 记忆兜底"的那些） */
  messages: number
  /** 其中屏上消息的条数 */
  screenMessages: number
  /** 其中由 contextHistory 兜底的条数 */
  contextFallback: number
  /** 被丢掉的失败气泡 */
  skippedErrors: number
  /** 被丢掉的空消息（没字也没图） */
  skippedEmpty: number
  /** 图片引用不是 dataURL（老导出包里的 ZIP 内路径）→ 图丢了、文字保留 */
  skippedImages: number
}

/** dataURL 图片判断：只认新后端 images.py 支持的四种 mime */
export function isSupportedDataUrlImage(value: string): boolean {
  return /^data:image\/(png|jpeg|jpg|webp|gif);base64,/i.test(value)
}

/** 一条本地消息 → 导入包消息；空消息 / 失败气泡返回 null */
function convertMessage(
  msg: LegacyMessage,
  stats: ConversionStats,
): ImportMessage | null {
  if (msg.isError) {
    stats.skippedErrors += 1
    return null
  }
  let image = msg.image
  let text = msg.text
  if (image && !isSupportedDataUrlImage(image)) {
    // ZIP 内路径（`cards/00/images/...`）在 JSON 包里后端读不到，只能放弃图；
    // 但"这里原本有张图"这件事要留下，否则消息会整条消失。
    stats.skippedImages += 1
    image = undefined
    if (!text.trim()) text = '[图片]'
  }
  if (!text.trim() && !image) {
    stats.skippedEmpty += 1
    return null
  }
  const out: ImportMessage = {
    // topic（群聊新话题）/ summary（对话总结卡片）都是"屏上能看、不该喂模型"的行，
    // 新后端用 side='system' 表达这件事（messages_repo 取上下文时会跳过 system）。
    role: msg.topic || msg.summary ? 'system' : msg.side === 'mine' ? 'user' : 'assistant',
    text: text || '[图片]',
  }
  if (msg.speakerName) out.speakerName = msg.speakerName
  if (msg.mood) out.mood = msg.mood
  if (image) out.image = image
  return out
}

function convertContext(
  entry: LegacyContextEntry,
  stats: ConversionStats,
): ImportContextEntry | null {
  let image = entry.image
  if (image && !isSupportedDataUrlImage(image)) {
    stats.skippedImages += 1
    image = undefined
  }
  if (!entry.text.trim()) return null
  const out: ImportContextEntry = {
    role: entry.side === 'mine' ? 'user' : 'assistant',
    text: entry.text,
  }
  if (image) out.image = image
  return out
}

/**
 * 本地数据 → `{version:1, cards:[...], myGender, promptOverrides}` 导入包
 *
 * 这个形状同时能直接喂给后端（`open_package` 按内容判断 JSON）和当作
 * 「先备份一份」下载的文件 —— 备份文件将来也能从这个入口导回来。
 */
export function buildImportPackage(local: LegacyLocalData): {
  package: ImportPackage
  stats: ConversionStats
} {
  const stats: ConversionStats = {
    cards: 0,
    conversations: 0,
    messages: 0,
    screenMessages: 0,
    contextFallback: 0,
    skippedErrors: 0,
    skippedEmpty: 0,
    skippedImages: 0,
  }
  const cards: ImportCard[] = []

  for (const card of local.cards) {
    const conversations: ImportConversation[] = []
    for (const conv of card.conversations) {
      const messages: ImportMessage[] = []
      for (const msg of conv.messages) {
        const converted = convertMessage(msg, stats)
        if (converted) messages.push(converted)
      }
      const contextHistory: ImportContextEntry[] = []
      for (const entry of conv.contextHistory ?? []) {
        const converted = convertContext(entry, stats)
        if (converted) contextHistory.push(converted)
      }
      // 名字为空后端会整段丢掉（_conversation 只认 name/title）；这里提前跳过，
      // 并把它的消息计入 skipped，表面上"少了一段"但至少数字说得清。
      if (!conv.name.trim()) {
        stats.skippedEmpty += messages.length
        continue
      }
      if (messages.length === 0 && contextHistory.length === 0) continue

      const outConv: ImportConversation = { name: conv.name, messages }
      if (contextHistory.length > 0) outConv.contextHistory = contextHistory
      conversations.push(outConv)

      stats.conversations += 1
      stats.screenMessages += messages.length
      if (messages.length === 0) stats.contextFallback += contextHistory.length
    }

    if (conversations.length === 0) continue
    const outCard: ImportCard = { conversations }
    // 群聊元数据是**卡片级**的：漏了它，导进去的群会退化成单聊壳（客户端踩过这个坑）
    if ((card.members?.length ?? 0) >= GROUP_MIN_MEMBERS) outCard.members = card.members
    if (card.myRole) outCard.myRole = card.myRole
    if (card.speakMode) outCard.speakMode = card.speakMode
    cards.push(outCard)
    stats.cards += 1
  }

  const pkg: ImportPackage = {
    version: 1,
    myGender: local.myGender === 'female' ? 'female' : 'male',
    cards,
  }
  // 只带真的有内容的覆盖项：空串会被后端 upsert 成"空提示词"，反而改坏角色
  const overrides: Record<string, string> = {}
  for (const [name, body] of Object.entries(local.promptOverrides ?? {})) {
    if (name && typeof body === 'string' && body.trim()) overrides[name] = body
  }
  if (Object.keys(overrides).length > 0) pkg.promptOverrides = overrides

  stats.messages = stats.screenMessages + stats.contextFallback
  return { package: pkg, stats }
}

// ---------------------------------------------------------------------------
// 上传 / 迁移编排（依赖全部注入，Node 自测里可以用假后端跑真 HTTP）
// ---------------------------------------------------------------------------

/** 给用户看的一句话错误分类（界面按它决定给什么下一步） */
export type MigrationErrorKind =
  | 'empty'
  | 'too_large'
  | 'network'
  | 'auth'
  | 'bad_package'
  | 'rate_limit'
  | 'storage_full'
  | 'server'
  | 'unknown'

export class LegacyMigrationError extends Error {
  readonly kind: MigrationErrorKind
  readonly status: number
  /** 后端返回的原始 message（用于日志/诊断，不直接甩给用户） */
  readonly detail: string
  /** 分批上传时：已经成功的批次数 / 总批次（失败重试时界面要说清楚不会重复导） */
  readonly donePieces: number
  readonly totalPieces: number

  constructor(
    kind: MigrationErrorKind,
    message: string,
    status = 0,
    detail = '',
    donePieces = 0,
    totalPieces = 0,
  ) {
    super(message)
    this.name = 'LegacyMigrationError'
    this.kind = kind
    this.status = status
    this.detail = detail
    this.donePieces = donePieces
    this.totalPieces = totalPieces
  }
}

export interface MigrationProgress {
  phase: 'reading' | 'uploading' | 'processing'
  /** 已上传字节（只有 uploading 阶段有） */
  loaded?: number
  /** 总字节（只有 uploading 阶段有） */
  total?: number
  /** 0~100；-1 = 算不出来（例如 fetch 没有上传进度） */
  percent?: number
  /** 第几批 / 共几批（超过 64MB 时自动分批，界面要能说"第 2/3 批"） */
  piece?: number
  pieces?: number
}

/** localStorage 的最小接口（测试里换成内存实现） */
export interface MarkerStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** 后端导入结果（`app/schemas.py` ImportResultOut） */
export interface ImportResult {
  id: string
  duplicate: boolean
  source: string
  mode: string
  package_version: number | string
  cards: number
  conversations: number
  messages: number
  images: number
  skipped: number
  notes: string[]
  created_at: string
  revoked_at?: string | null
}

/** 写进 localStorage 的迁移标记 */
export interface MigrationMarker {
  batchId: string
  /** 分批上传时的全部批次号（单批时为空/只有一项） */
  batchIds?: string[]
  at: number
  counts: {
    cards: number
    conversations: number
    messages: number
    images: number
    skipped: number
  }
}

export interface MigrationDeps {
  /** 读本地老数据（浏览器里是 IndexedDB + localStorage；测试里是手写样本） */
  readLocal: () => Promise<LegacyLocalData | null>
  /** 确保手里有设备 token */
  ensureToken: () => Promise<string>
  /** `POST /v1/imports` 的完整地址 */
  importUrl: string
  /** 标记读写；不传 = 只在本进程里判断（永远当作没迁过） */
  storage?: MarkerStorage | null
  /** 上传用的 fetch（默认全局 fetch；Node 22 / 浏览器都有） */
  fetchImpl?: typeof fetch
  /** 忽略"已迁移"标记，强制再传一次（用户点「重试」/ 想再确认一遍时用） */
  force?: boolean
  /** 进度回调（界面画进度条） */
  onProgress?: (progress: MigrationProgress) => void
  /** 当前时间（测试可固定） */
  now?: () => number
  /** 体积上限，默认 64MB（与后端一致） */
  maxBytes?: number
  /** 上传文件名（后端按内容判断类型，名字只用于导入记录） */
  filename?: string
}

export type MigrationOutcome =
  | {
      skipped: true
      /** 之前迁移过的标记 */
      marker: MigrationMarker | null
    }
  | {
      skipped: false
      duplicate: boolean
      batchId: string
      batchIds: string[]
      counts: MigrationMarker['counts']
      notes: string[]
      marker: MigrationMarker
      /** 标记是否真的写进了 localStorage（隐私模式可能写不进） */
      markerWritten: boolean
      /** 本地上传的包大小（字节，分批时是合计） */
      packageBytes: number
      /** 分了几批上传（1 = 没超 64MB） */
      pieces: number
      /** 本地换算出来的统计（与后端返回的数字对账用） */
      local: ConversionStats
      result: ImportResult
    }

/** 读迁移标记；坏值当作没迁过（宁可再导一次，也不能误判成已迁移） */
export function readMigrateMarker(storage?: MarkerStorage | null): MigrationMarker | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(MIGRATE_MARKER_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (!isRecord(parsed)) return null
    const batchId = str(parsed.batchId)
    if (!batchId) return null
    const at = finiteNumber(parsed.at) ?? 0
    const countsRaw = isRecord(parsed.counts) ? parsed.counts : {}
    const numberOrZero = (value: unknown): number => finiteNumber(value) ?? 0
    return {
      batchId,
      at,
      counts: {
        cards: numberOrZero(countsRaw.cards),
        conversations: numberOrZero(countsRaw.conversations),
        messages: numberOrZero(countsRaw.messages),
        images: numberOrZero(countsRaw.images),
        skipped: numberOrZero(countsRaw.skipped),
      },
    }
  } catch {
    return null
  }
}

export function writeMigrateMarker(
  storage: MarkerStorage | null | undefined,
  marker: MigrationMarker,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(MIGRATE_MARKER_KEY, JSON.stringify(marker))
    return true
  } catch {
    // 配额满 / 隐私模式：迁移本身已经成功，标记丢了最多下次再传一遍（后端按包哈希去重）
    return false
  }
}

/** 从后端错误体里抠出 code / message（读不出来就返回空） */
function readErrorBody(text: string): { code: string; message: string } {
  try {
    const parsed: unknown = JSON.parse(text)
    if (isRecord(parsed) && isRecord(parsed.error)) {
      return { code: str(parsed.error.code), message: str(parsed.error.message) }
    }
  } catch {
    // 网关 HTML / 空响应
  }
  return { code: '', message: '' }
}

function errorForStatus(status: number, text: string): LegacyMigrationError {
  const body = readErrorBody(text)
  const detail = body.message || body.code || ''
  if (status === 413) {
    return new LegacyMigrationError('too_large', '数据包超过服务器单次导入上限 64MB', status, detail)
  }
  if (status === 401 || status === 403) {
    return new LegacyMigrationError('auth', '设备凭证无效或没有权限', status, detail)
  }
  if (status === 400) {
    return new LegacyMigrationError(
      'bad_package',
      detail || '服务器读不出这份数据包',
      status,
      detail,
    )
  }
  if (status === 429) {
    return new LegacyMigrationError('rate_limit', '操作太频繁，被服务器限流', status, detail)
  }
  if (status === 507) {
    return new LegacyMigrationError('storage_full', '服务器存储空间不足', status, detail)
  }
  if (status >= 500) {
    return new LegacyMigrationError('server', `服务器出错（HTTP ${status}）`, status, detail)
  }
  return new LegacyMigrationError('unknown', detail || `导入失败（HTTP ${status}）`, status, detail)
}

interface UploadArgs {
  url: string
  blob: Blob
  filename: string
  token: string
  fetchImpl?: typeof fetch
  onProgress?: (progress: MigrationProgress) => void
}

/** 把包 POST 到 /v1/imports；返回状态与响应文本（错误分类交给调用方） */
async function postImportPackage(args: UploadArgs): Promise<{ status: number; text: string }> {
  const { url, blob, filename, token, fetchImpl, onProgress } = args
  const form = new FormData()
  form.append('file', blob, filename)
  form.append('mode', 'merge')

  // 浏览器里用 XHR：fetch 拿不到上传进度，几十 MB 的包没有进度条就像卡死
  if (!fetchImpl && typeof XMLHttpRequest !== 'undefined') {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', url, true)
      xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      // 不设 Content-Type：浏览器要自己带 multipart boundary
      xhr.timeout = 600_000
      if (xhr.upload) {
        xhr.upload.onprogress = (event) => {
          if (!event.lengthComputable) return
          onProgress?.({
            phase: 'uploading',
            loaded: event.loaded,
            total: event.total,
            percent: event.total > 0 ? Math.min(99, Math.round((event.loaded / event.total) * 100)) : -1,
          })
        }
        xhr.upload.onload = () => {
          onProgress?.({ phase: 'processing', percent: 100 })
        }
      }
      xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText ?? '' })
      xhr.onerror = () => reject(new LegacyMigrationError('network', '上传失败：连不上服务器'))
      xhr.ontimeout = () => reject(new LegacyMigrationError('network', '上传超时'))
      xhr.onabort = () => reject(new LegacyMigrationError('network', '上传已取消'))
      xhr.send(form)
    })
  }

  const doFetch = fetchImpl ?? fetch
  onProgress?.({ phase: 'uploading', loaded: 0, total: blob.size, percent: -1 })
  let response: Response
  try {
    response = await doFetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    })
  } catch {
    throw new LegacyMigrationError('network', '上传失败：连不上服务器')
  }
  onProgress?.({ phase: 'processing', percent: 100 })
  return { status: response.status, text: await response.text() }
}

/**
 * 跑一次迁移
 *
 * 顺序：检查标记 → 读本地 → 拼包（体积检查）→ 确保 token → 上传 → 写标记。
 * **失败不写标记**，所以下次还能重试；成功才写，避免每次启动都重复上传。
 * 注意：这里绝不删改任何老数据 —— 读的都是同一个库，写入的只有新 key 的标记。
 */
/** JSON.stringify 后的**真实字节数**（UTF-8；`str.length` 对中文会低估） */
function jsonBytes(value: unknown): number {
  return new Blob([JSON.stringify(value)]).size
}

/** 一个空包（分批时每批都要带 version / myGender） */
function emptyPackage(local: LegacyLocalData): ImportPackage {
  return {
    version: 1,
    myGender: local.myGender === 'female' ? 'female' : 'male',
    cards: [],
  }
}

/**
 * 把一个包切成若干份，每份都 ≤ 上限（自动分批导入）
 *
 * 为什么必须真的能分批：后端 `/v1/imports` 硬上限 64MB，老用户的历史里只要有
 * 几十张截图就会超。只回一句"太大了"对用户没有任何用 —— 他既不能删老数据
 * （那是唯一副本），也不一定愿意去「数据管理」手工导。这里按粒度从粗到细拆：
 *
 *   整包 → 按卡片 → 按子对话 → 按消息
 *
 * 每份都是一个独立、合法的小包；后端按包内容哈希各自幂等，重试不会重复建会话。
 * 拆分只影响"分几批上传"，不影响任何一条消息的顺序（同段对话按原数组顺序切片）。
 *
 * 极端情况：**单独一条消息**就超过上限（多半是一张几十 MB 的图）。这种没法再拆，
 * 抛 too_large，由界面提示"先导出文件/清理大图再导"。
 */
export function splitImportPackage(
  pkg: ImportPackage,
  local: LegacyLocalData,
  maxBytes: number,
): ImportPackage[] {
  // 留 5% 余量：multipart 的边界、字段名、文件名也占字节
  const limit = Math.max(1024, Math.floor(maxBytes * 0.95))
  if (jsonBytes(pkg) <= limit) return [pkg]

  /** 拆到不可再拆的"单元"：每张卡片 / 单段对话 / 单段对话的消息切片 */
  const units: ImportCard[] = []
  const oversizedUnit = (): never => {
    throw new LegacyMigrationError(
      'too_large',
      '有条消息本身超过单次导入上限（多半是一张几十 MB 的图片），没法自动分批',
    )
  }

  for (const card of pkg.cards) {
    const meta: ImportCard = { conversations: [] }
    if (card.members) meta.members = card.members
    if (card.myRole) meta.myRole = card.myRole
    if (card.speakMode) meta.speakMode = card.speakMode

    // 整张卡装得下就整张走，少切几刀、少建几条"重复名字"的会话
    if (jsonBytes({ ...meta, conversations: card.conversations }) <= limit - 2048) {
      units.push({ ...meta, conversations: card.conversations })
      continue
    }

    for (const conv of card.conversations) {
      if (jsonBytes({ ...meta, conversations: [conv] }) <= limit - 2048) {
        units.push({ ...meta, conversations: [conv] })
        continue
      }
      // 单段对话还超限：按消息顺序切片；contextHistory 跟在最后一片上传
      let batch: ImportMessage[] = []
      const flush = () => {
        if (batch.length === 0) return
        units.push({ ...meta, conversations: [{ name: conv.name, messages: batch }] })
        batch = []
      }
      for (const msg of conv.messages) {
        batch.push(msg)
        if (jsonBytes({ ...meta, conversations: [{ name: conv.name, messages: batch }] }) > limit - 2048) {
          batch.pop()
          if (batch.length === 0) oversizedUnit()
          flush()
          batch.push(msg)
        }
      }
      flush()
      if (conv.contextHistory && conv.contextHistory.length > 0 && units.length > 0) {
        // 最后一片补上 AI 记忆：拆开之后屏上消息非空，后端只会把它计入
        // dropped_context（不会重复进消息），所以不会造成"消息翻倍"
        const last = units[units.length - 1]
        const lastConv = last.conversations[0]
        if (lastConv && lastConv.name === conv.name) lastConv.contextHistory = conv.contextHistory
      }
    }
  }

  // 把单元贪心装进批次
  const pieces: ImportPackage[] = []
  let current = emptyPackage(local)
  let currentSize = jsonBytes(current)
  for (const unit of units) {
    const unitSize = jsonBytes(unit)
    if (current.cards.length > 0 && currentSize + unitSize + 1 > limit) {
      pieces.push(current)
      current = emptyPackage(local)
      currentSize = jsonBytes(current)
    }
    current.cards.push(unit)
    currentSize += unitSize + 1
  }
  if (current.cards.length > 0) pieces.push(current)

  // 自定义提示词只放第一批：device_prompts 是 upsert，重复传也不会坏，但没必要每批都带
  if (pkg.promptOverrides && pieces.length > 0) pieces[0].promptOverrides = pkg.promptOverrides
  return pieces
}

export async function runMigration(deps: MigrationDeps): Promise<MigrationOutcome> {
  const now = deps.now ?? (() => Date.now())
  const storage = deps.storage ?? null

  if (!deps.force) {
    const existing = readMigrateMarker(storage)
    if (existing) return { skipped: true, marker: existing }
  }

  deps.onProgress?.({ phase: 'reading' })
  const local = await deps.readLocal()
  if (!local) {
    throw new LegacyMigrationError('empty', '没有找到旧版本地数据')
  }
  const { package: pkg, stats } = buildImportPackage(local)
  if (pkg.cards.length === 0) {
    throw new LegacyMigrationError('empty', '旧数据里没有可迁移的聊天记录')
  }

  const maxBytes = deps.maxBytes ?? IMPORT_MAX_BYTES
  // 超过 64MB 不报错、自动分批（见 splitImportPackage 的注释）
  const pieces = splitImportPackage(pkg, local, maxBytes)

  const token = await deps.ensureToken()
  const stamp = timestampForFile(now())
  const counts: MigrationMarker['counts'] = {
    cards: 0,
    conversations: 0,
    messages: 0,
    images: 0,
    skipped: 0,
  }
  const notes: string[] = []
  const batchIds: string[] = []
  let packageBytes = 0
  let duplicateAll = true
  let lastResult: ImportResult | null = null

  for (let index = 0; index < pieces.length; index += 1) {
    const piece = pieces[index]
    const pieceJson = JSON.stringify(piece)
    const blob = new Blob([pieceJson], { type: 'application/json' })
    packageBytes += blob.size
    const pieceNo = index + 1

    const response = await postImportPackage({
      url: deps.importUrl,
      blob,
      filename:
        pieces.length > 1
          ? `BAKER-legacy-${stamp}-p${pieceNo}of${pieces.length}.json`
          : `BAKER-legacy-${stamp}.json`,
      token,
      fetchImpl: deps.fetchImpl,
      onProgress: (progress) =>
        deps.onProgress?.({ ...progress, piece: pieceNo, pieces: pieces.length }),
    })

    if (response.status !== 200 && response.status !== 201) {
      const err = errorForStatus(response.status, response.text)
      // 带上"成功了几批"：重试时前面的批次会被后端按包哈希去重，不会重复建会话
      throw new LegacyMigrationError(
        err.kind,
        err.message,
        err.status,
        err.detail,
        index,
        pieces.length,
      )
    }

    let result: ImportResult
    try {
      result = JSON.parse(response.text) as ImportResult
    } catch {
      throw new LegacyMigrationError(
        'server',
        '导入成功但服务器返回了看不懂的内容',
        0,
        '',
        index,
        pieces.length,
      )
    }
    if (!result || typeof result.id !== 'string') {
      throw new LegacyMigrationError(
        'server',
        '导入成功但服务器没有返回批次号',
        0,
        '',
        index,
        pieces.length,
      )
    }

    lastResult = result
    batchIds.push(result.id)
    duplicateAll = duplicateAll && !!result.duplicate
    counts.cards += result.cards ?? 0
    counts.conversations += result.conversations ?? 0
    counts.messages += result.messages ?? 0
    counts.images += result.images ?? 0
    counts.skipped += result.skipped ?? 0
    for (const note of Array.isArray(result.notes) ? result.notes : []) {
      const text = String(note)
      if (!notes.includes(text)) notes.push(text)
    }
  }

  if (!lastResult) throw new LegacyMigrationError('server', '没有上传任何数据')

  const marker: MigrationMarker = {
    batchId: batchIds[batchIds.length - 1] ?? lastResult.id,
    batchIds,
    at: now(),
    counts,
  }
  const markerWritten = writeMigrateMarker(storage, marker)

  return {
    skipped: false,
    duplicate: duplicateAll,
    batchId: marker.batchId,
    batchIds,
    counts,
    notes,
    marker,
    markerWritten,
    packageBytes,
    pieces: pieces.length,
    local: stats,
    result: lastResult,
  }
}

/** 文件名时间戳（YYYYMMDD-HHmmss，与老导出包同一个口径，方便用户对时间） */
export function timestampForFile(ms: number): string {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  )
}

// ---------------------------------------------------------------------------
// 给人看的文案（界面直接用；错误分类 → 下一步动作）
// ---------------------------------------------------------------------------

/** 迁移结果的摘要（会话 / 消息 / 图片 / 跳过；重复导入单独说一句） */
export function describeMigration(
  outcome: Extract<MigrationOutcome, { skipped: false }>,
): string {
  const c = outcome.counts
  const parts = [`${c.conversations} 段会话`, `${c.messages} 条消息`]
  if (c.images > 0) parts.push(`${c.images} 张图片`)
  if (c.cards > 0) parts.push(`${c.cards} 个角色/群聊`)
  if (c.skipped > 0) parts.push(`服务器跳过 ${c.skipped} 项`)
  const head = outcome.duplicate
    ? '这份数据之前已经导过（服务器按内容去重，没有重复导入）：'
    : outcome.pieces > 1
      ? `已分 ${outcome.pieces} 批迁移：`
      : '已迁移：'
  const local = outcome.local
  const localParts: string[] = []
  if (local.skippedErrors > 0) localParts.push(`${local.skippedErrors} 条失败气泡`)
  if (local.skippedEmpty > 0) localParts.push(`${local.skippedEmpty} 条空消息`)
  if (local.skippedImages > 0) localParts.push(`${local.skippedImages} 张读不出的图片`)
  const tail = localParts.length > 0 ? `（本地另跳过 ${localParts.join('、')}）` : ''
  return `${head}${parts.join(' · ')}${tail}`
}

/** 错误 → 一句"照着做就能成"的话 */
export function describeMigrationError(err: unknown, bytes?: number): string {
  if (err instanceof LegacyMigrationError) {
    const mb = bytes && bytes > 0 ? `（约 ${(bytes / 1024 / 1024).toFixed(1)}MB）` : ''
    // 分批上传时前面已经有成功的批次：必须说清楚"重试不会重复导"，
    // 否则用户不敢点重试（怕导两遍），或者以为前几批白导了。
    const partial =
      err.donePieces > 0 && err.totalPieces > 0
        ? `已经成功导入 ${err.donePieces}/${err.totalPieces} 批；点「重试」会从第 ${err.donePieces + 1} 批继续（前面的批次服务器会按内容去重，不会重复建会话）。`
        : ''
    switch (err.kind) {
      case 'too_large':
        return (
          `本地数据${mb}太大，自动分批也装不下一整条记录（多半是单张几十 MB 的图片）。` +
          '可行路径（任选一条）：' +
          '① 先在「设置 → 数据管理 → 导出数据」生成 ZIP 文件，再用「导入数据」导入（ZIP 会把图片单独打包，通常能小很多）；' +
          '② 在「数据管理」里清理几张特别大的图片后，回到这里点「重试」。'
        )
      case 'network':
        return `连不上服务器：请检查网络后点「重试」。本地数据没有被改动。${partial}`
      case 'auth':
        return `设备凭证失效了：请刷新页面后再试；如果仍失败，到「设置 → 账号」里检查登录状态。${partial}`
      case 'bad_package':
        return (
          '服务器读不出这份本地数据（可能有损坏的卡片）。请先点「先备份一份」把原始数据存下来，' +
          '再用「设置 → 数据管理 → 导出数据」生成文件后导入。'
        )
      case 'rate_limit':
        return `操作太频繁了，服务器在限流：等一分钟左右再点「重试」。${partial}`
      case 'storage_full':
        return `服务器存储空间满了，暂时写不进去：过一会儿再试，或联系管理员。${partial}`
      case 'server':
        return `服务器出错了（不是你的数据问题）：稍后点「重试」；反复失败请把这句话反馈给开发者。${partial}`
      case 'empty':
        return err.message
      default:
        return `${err.message || '迁移失败，请重试'}${partial ? `\n${partial}` : ''}`
    }
  }
  return err instanceof Error ? err.message : String(err)
}
