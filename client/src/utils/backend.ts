// =============================================================================
// backend.ts —— 单聊中间层（新后端 /v1 契约）
// -----------------------------------------------------------------------------
// 旧版这一层是"无状态网关"：每次把最近 15~25 轮 history 整个传上去，后端
// 只负责拼提示词和出话。**新后端的架构反过来**：会话和消息的真相在服务端，
// 客户端只给「会话指针 + 新事件 + 幂等键」（见 web/back/README.md）。
//
// 但界面（useAiChat 等）是按旧契约写的：它把 history 交给我们、拿回一段回复，
// 自己不持有任何服务端 id。所以这一层的职责就是**把两种模型缝起来**：
//
//   UI 视角                中间层做的事                      新后端
//   ───────────────────────────────────────────────────────────────
//   history[]      ──►  第一次见到某个角色时，把 history      POST /v1/imports
//                        一次性导入成服务端会话（幂等键=包哈希）
//   message + 图片 ──►  当前轮的图先上传成附件              POST /v1/attachments
//                        再写一条消息（client_msg_id 幂等）   POST /v1/conversations/{id}/messages
//   {reply}        ◄──  读服务端回复（重发时按 seq 回放）    GET  .../messages?after_seq=
//   SSE            ──►  同一个消息端点，换成 :stream        POST .../messages:stream
//
// 设计上的三个硬约束（都是新后端定的，不要绕）：
//   1. **会话按"这段对话"绑定**：绑定键 = 角色 + 这段对话的 id
//      （`Conversation.localId`；服务端列表模式下用 `r:<服务端会话 id>`）。
//      ⚠️ 2026-11-19 之前这里只认角色名，于是同角色的两段对话共用一条服务端会话
//      ——「第 1 段叫了声亲爱的，第 2 段无缝接上」，群友实测 100% 复现。
//      现在一个存档一条会话；回归见 `scripts/two-archives-e2e.mjs`。
//   2. **幂等键必须跨"流式失败 → 非流式回退"保持稳定**：否则一次失败会写两条
//      用户消息。这里用「会话 + 文本 + 历史长度」做 key，成功后才清掉。
//   3. **历史只导入一次**：此后服务端自己记；再传 history 会被忽略（也不会重复导入，
//      因为导入接口按包内容哈希幂等）。
//
// 自定义 API 模式（apiMode === 'custom'）不经过本模块，由 utils/llm.ts 直连。
// =============================================================================

import type { ApiConfig } from '../stores/settings'
import { v1Url } from '../constants/apiRoutes'
import {
  apiFetch,
  ApiError,
  ensureDeviceToken,
  readError,
} from './apiClient'
import { devInfo, devWarn } from './logger'
// 新建会话时的「本局记忆」选择（设置 → 干员记忆 → 新建对话，存 localStorage）。
// 后端只在建会话这一刻收 memory 字段，之后没有改已有会话的接口。
import { readNewChatMemory } from './memoryApi'
// 每发一条消息都带「时间感知」：关掉时既发 time_enabled:false，也把 local_time 发空
import { readTimeAware } from './timeAwareness'
import { SUMMARY_ENTRY_PREFIX } from './contextBudget'

// ---------------------------------------------------------------------------
// 对外类型（与旧版保持兼容：useAiChat / 设置弹窗按这些字段读写）
// ---------------------------------------------------------------------------

/** 后端请求的历史条目（OpenAI 风格 role/content） */
export interface BackendHistoryEntry {
  role: 'user' | 'assistant'
  content: string
  /** 图片 dataURL（识图用；无图片时缺省） */
  image?: string
}

/**
 * 后端请求体（旧契约形状）
 *
 * 新中间层不再把它原样发出去，而是拆成「导入 / 上传 / 发消息」三步。
 * 保留这个类型是为了让 useAiChat 的调用处一行都不用改。
 */
export interface BackendRequest {
  /** 角色 ID（新协议主字段；后端据此加载角色提示词） */
  character_id?: string
  /** 角色中文名（旧字段，后端兼容） */
  character?: string
  /** 旧版兼容：完整提示词（现在只用于"未收录角色"的日志，不发送） */
  system_prompt?: string
  /** 用户自定义覆盖的角色提示词（新后端没有这个入口，见 docs/02 §7） */
  character_prompt?: string
  /** 当前用户输入（不重复出现在 history 中） */
  message: string
  /** 最近的历史条目（第一次见到该角色时会被导入服务端） */
  history: BackendHistoryEntry[]
  // 思考模式
  think?: boolean
  /**
   * 这段对话的稳定本地 id（`types/chat.ts` 的 `Conversation.localId`）
   *
   * 绑定服务端会话时用它做键 —— 同一个角色的两段对话必须落两条会话，
   * 否则"第 1 段叫了声亲爱的，第 2 段无缝接上"（2026-11-19 修）。
   */
  conversation_key?: string
  /** 沉浸式：不显示括号动作描写（服务端后处理） */
  immersive_mode?: boolean
  /** 实验性：新版提示词（新后端没有这个开关，保留字段只为兼容调用处） */
  use_new_prompt?: boolean
  // 【2026-11-18 P0】删除的字段，原因各一行：
  //   force_search / use_rag —— 新后端 capabilities.web_search=false，只走世界书，
  //     发过去也会被忽略；UI 开关已一并下线。
  //   need_summary / context_window_tokens / summary_context —— 服务端每轮自己压缩
  //     （memory.compress_to_fit），前端再要一次总结只会变成第二条压缩链。
}

/** 后端上报的 token 用量 */
export interface BackendUsage {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
  cached_tokens?: number
  total_ms?: number
  model?: string
  api_id?: string
}

/** 后端响应体（旧形状；本层组装出来给 UI） */
export interface BackendReply {
  reply: string
  /** 心情表情 token（新后端把表情翻译成文字，不再单独下发 token） */
  mood?: string
  usage?: BackendUsage
  /** 本次实际生效的知识源 */
  knowledge_source?: string
  // 【2026-11-18 P0】原来的 summary 字段已删除：新后端不返回总结，
  // 前端也不再请求（压缩全在服务端）。
}

/** fetchBackendReply 的解析结果 */
export interface BackendReplyResult {
  reply: string
  mood?: string
  usage?: BackendUsage
  knowledgeSource?: string
}

/**
 * 「流式不可用」专用错误（收到任何 delta **之前**就失败）
 *
 * 调用方（useAiChat）据此回退到非流式一次。**只有链路级失败**才抛它；
 * 服务端明确报错（429 / 上游失败）原样抛，避免"回退 = 重发 = 重复占额度"。
 */
export class BackendStreamUnavailableError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'BackendStreamUnavailableError'
  }
}

/** 流式回调 */
export interface BackendStreamOptions {
  /** 收到一个增量片段 */
  onDelta?: (text: string) => void
  /** 阶段事件（新后端没有阶段帧，保留字段只为兼容调用处） */
  onStage?: (stage: string) => void
  // 【2026-11-18 P0】onSummary 已删除：新后端没有总结帧。
  /** 中止信号 */
  signal?: AbortSignal
}

/** 推荐输入 */
export interface BackendSuggestionInput {
  characterId?: string
  character?: string
  message?: string
  /** 这段对话的稳定本地 id（与发送链路同一把键，别让推荐去猜会话） */
  conversationKey?: string
  history: Array<{ side: 'other' | 'mine'; text: string; image?: string }>
  useNewPrompt?: boolean
  immersiveMode?: boolean
}

// ---------------------------------------------------------------------------
// 角色 id
// ---------------------------------------------------------------------------

/**
 * 角色中文名 → slug（/v1/characters 的 id）
 *
 * 服务端是权威（36 个角色），启动后会异步拉一次覆盖这张表；
 * 这里保留静态表是因为 UI 在同步路径上就要用到（渲染前就要知道 id）。
 */
export const CHARACTER_ID_MAP: Record<string, string> = {
  伊冯: 'yifeng',
  余烬: 'yujin',
  佩丽卡: 'perlica',
  别礼: 'bieli',
  卡契尔: 'kaqier',
  卡缪: 'camille',
  埃特拉: 'aitela',
  大潘: 'dapan',
  安塔尔: 'antaer',
  庄方宜: 'zhuangfangyi',
  弧光: 'huguang',
  弭弗: 'mifei',
  昼雪: 'zhouxue',
  梨诺: 'linuo',
  汤汤: 'tangtang',
  洁尔佩塔: 'jieerpeita',
  洛茜: 'luoxi',
  狼卫: 'langwei',
  秋栗: 'qiuli',
  艾尔黛拉: 'aierdaila',
  艾维文娜: 'aiweiwena',
  莱万汀: 'laiwanting',
  萤石: 'yingshi',
  诀: 'jue',
  赛希: 'saixi',
  阿列什: 'alieshi',
  陈千语: 'chenqianyu',
  骏卫: 'junwei',
  黎风: 'lifeng',
  聂菲斯: 'niefeisi',
  阿达希尔: 'adaxier',
  提弗洛斯: 'tifuluosi',
  明河: 'minghe',
  祀: 'si',
  噗切娜: 'puqiena',
  管理员: 'endministrator',
}

/** 角色 ID 列表（遍历用） */
export const CHARACTER_IDS: string[] = Object.values(CHARACTER_ID_MAP)

/** 服务端角色表是否已经同步过（只同步一次） */
let charactersSynced = false

/**
 * 拉一次服务端角色表，覆盖 name → slug
 *
 * 失败不影响聊天（静态表兜底）；成功的唯一价值是"服务端新加的角色"也能认。
 * 不 await：UI 的同步路径不能等网络。
 */
function syncCharactersOnce(): void {
  if (charactersSynced) return
  charactersSynced = true
  void (async () => {
    try {
      const response = await apiFetch('/characters', { method: 'GET' })
      if (!response.ok) return
      const data = (await response.json()) as {
        items?: Array<{ id?: string; name?: string }>
      }
      for (const item of data.items ?? []) {
        if (item.name && item.id) CHARACTER_ID_MAP[item.name] = item.id
      }
    } catch (err) {
      devWarn('[backend] 角色表同步失败，使用内置表:', err)
    }
  })()
}

/** 解析后端根地址（设置里的 backendUrl 优先，其次 apiRoutes 的默认值） */
export function resolveBackendUrl(_character: string, fallback = ''): string {
  const base = (fallback || '').trim()
  if (base) return base.replace(/\/+$/, '')
  return v1Url('') // `{base}/v1`
}

/** 最近保留的问答轮数（第一次导入时最多带这么多） */
export const BACKEND_HISTORY_ROUNDS = 15

/** 最近保留的历史条数（15 轮 × 2） */
export const BACKEND_HISTORY_LIMIT = BACKEND_HISTORY_ROUNDS * 2

// ---------------------------------------------------------------------------
// 组装请求（保留旧函数，UI 调用处不改）
// ---------------------------------------------------------------------------

export function buildBackendRequest(
  message: string,
  character: string,
  history: Array<{ side: 'other' | 'mine'; text: string; image?: string }>,
  options?: {
    characterId?: string
    characterPromptOverride?: string
    systemPrompt?: string
    think?: boolean
    immersiveMode?: boolean
    useNewPrompt?: boolean
    /** 这段对话的稳定本地 id（缺省 = 老调用点，按角色兜底绑定） */
    conversationKey?: string
  },
): BackendRequest {
  // 头部那条【对话总结】不是"谁说的话"：不导入服务端（服务端有自己的摘要），
  // 也不当普通历史 —— 老的本地存档里可能还留着以前前端生成的总结头，
  // 直接丢掉（新后端自己会压，不需要它，也不该把它当玩家说的一句话）。
  let dialogHistory = history
  const head = history[0]
  if (head && typeof head.text === 'string' && head.text.startsWith(SUMMARY_ENTRY_PREFIX)) {
    dialogHistory = history.slice(1)
  }

  const entries: BackendHistoryEntry[] = dialogHistory
    .slice(-BACKEND_HISTORY_LIMIT)
    .map((h) => ({
      role: h.side === 'mine' ? 'user' : 'assistant',
      content: h.image ? '[图片]' : h.text,
      image: h.image || undefined,
    }))

  const characterId = options?.characterId || CHARACTER_ID_MAP[character] || ''

  const req: BackendRequest = {
    message,
    history: entries,
    character,
    think: options?.think ?? false,
    immersive_mode: options?.immersiveMode ?? true,
    use_new_prompt: options?.useNewPrompt ?? false,
  }
  // 这段对话的稳定 id：决定"发到哪一条服务端会话"（见 ensureConversation）
  if (options?.conversationKey) req.conversation_key = options.conversationKey
  if (characterId) {
    req.character_id = characterId
    if (options?.characterPromptOverride) req.character_prompt = options.characterPromptOverride
  } else if (options?.systemPrompt) {
    req.system_prompt = options.systemPrompt
  }
  return req
}

// ---------------------------------------------------------------------------
// 服务端会话绑定（角色 → conversation public_id）
// ---------------------------------------------------------------------------

interface Binding {
  /** 服务端会话 public_id */
  id: string
  /** 绑定的时间戳（排查用） */
  at: number
}

const BINDINGS_KEY = 'baker_v1_bindings'

/**
 * 绑定键：**角色 + 这段对话的 id**
 *
 * 2026-11-19 修「两段对话互相串」：以前键只有角色 id，
 * 于是同一角色的第 1 段和第 2 段对话共用同一条服务端会话
 * （第 1 段叫了声「亲爱的」，第 2 段直接接上）。
 * 现在带上 `localId`，一个存档一条会话。
 *
 * `localId` 为空时退回纯角色名 —— 只给"没有对话对象"的老调用点兜底，
 * 主链路（useAiChat）一定会带上。
 */
function bindingKey(characterId: string, localId?: string): string {
  return localId ? `${characterId}#${localId}` : characterId
}

function readBindings(): Record<string, Binding> {
  try {
    const raw = localStorage.getItem(BINDINGS_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, Binding>
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeBindings(map: Record<string, Binding>): void {
  try {
    localStorage.setItem(BINDINGS_KEY, JSON.stringify(map))
  } catch {
    // 静默失败：本次会话仍能聊，只是刷新后要重新绑一次
  }
}

function dropBinding(characterId: string): void {
  const map = readBindings()
  if (map[characterId]) {
    delete map[characterId]
    writeBindings(map)
  }
}

/**
 * 手动把某个角色绑定到指定的服务端会话（P1 列表服务端驱动用）
 *
 * 为什么还需要它：发送链路（ensureConversation）仍然按「角色 → 会话」找会话。
 * 列表服务端驱动之后，用户在界面上点的是**具体某一段会话**，所以点选时
 * 把绑定指向它 —— 这样"发消息发到哪一段"由点击决定，而不是靠猜。
 *
 * @param characterId 角色 slug（/v1/characters 的 id）
 * @param conversationId 服务端会话 id
 */
export function bindConversation(
  characterId: string,
  conversationId: string,
  localId?: string,
): void {
  if (!characterId || !conversationId) return
  const key = bindingKey(characterId, localId)
  const map = readBindings()
  const current = map[key]
  if (current?.id === conversationId) return
  map[key] = { id: conversationId, at: Date.now() }
  writeBindings(map)
}

/**
 * 忘掉全部「角色 → 服务端会话」绑定
 *
 * 什么时候用：领完取件码 / 注册时带了码 —— 后端那边**新建**了一条会话装着
 * 刚导入的历史，而本地绑定表还指着旧的（空的）会话。不清掉的话，
 * 下一句话会接在旧会话上，等于"领了却看不见"。
 * 清掉之后，下一次发消息会按"服务器上最近活跃的那条"重新绑定（见 ensureConversation）。
 *
 * 只清单聊绑定，不动群聊别名表（baker_v1_group_alias）：那边有自己的对应关系。
 */
export function forgetAllBindings(): void {
  try {
    localStorage.removeItem(BINDINGS_KEY)
  } catch {
    // 隐私模式 / 存储被禁用：没有绑定时本来就是"每次重新找"，不清理也没事
  }
}

/** 正在绑定中的角色（并发请求共用一次绑定，避免建出两条会话） */
const bindingLocks = new Map<string, Promise<Binding>>()

/** 服务端会话形状（只取用到的字段） */
interface ConversationOut {
  id: string
  character_id: string | null
  mode: string
  title: string
  next_seq: number
  last_active_at?: string
}

/**
 * 找到这个角色最近的一条单聊会话
 *
 * 为什么要"找"而不是每次建：localStorage 被清 / 换浏览器之后，服务端会话还在；
 * 直接建新的会让上下文从零开始（玩家会觉得"她怎么不记得了"）。
 */
async function listConversations(): Promise<ConversationOut[]> {
  const response = await apiFetch('/conversations', { method: 'GET' })
  if (!response.ok) throw await readError(response)
  const data = (await response.json()) as { items?: ConversationOut[] }
  return data.items ?? []
}

async function findConversation(characterId: string): Promise<ConversationOut | null> {
  const items = await listConversations()
  const candidates = items.filter(
    (item) => item.mode === 'single' && item.character_id === characterId,
  )
  if (candidates.length === 0) return null
  // 列表本身按最近活跃倒序；这里再按 next_seq 兜一层，防后端排序变化
  candidates.sort((a, b) => (b.next_seq ?? 0) - (a.next_seq ?? 0))
  return candidates[0] ?? null
}

/** 建一条单聊会话 */
async function createConversation(
  characterId: string,
  characterName: string,
): Promise<ConversationOut> {
  const response = await apiFetch('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character_id: characterId,
      mode: 'single',
      title: characterName,
      // inherit = 跟随全局开关；on/off = 这一局单独覆盖（本地偏好，见 memoryApi）
      memory: readNewChatMemory(),
    }),
  })
  if (!response.ok) throw await readError(response)
  return (await response.json()) as ConversationOut
}

/** 导入包里的单条消息 */
interface ImportMessage {
  side: 'mine' | 'other'
  text: string
}

/** 生成摘要的占位行（前端"智能总结"产物）—— 不当成真人说过的话导入 */
const SUMMARY_PREFIX = '【对话总结】'

/**
 * 把本地历史导入成一条服务端会话
 *
 * 用 `/v1/imports` 而不是逐条 `POST /messages`：后者只能写 user 侧
 * （服务端把"用户消息"和"助手回复"分开落库），逐条写会丢掉所有助手回复，
 * 模型看到的上下文就只剩玩家单方面说话。导入接口是官方给老数据包的口子，
 * 两种 side、图片、说话人都能进。
 *
 * 包内容哈希是幂等键：同一个包传两次不会导两遍。
 */
async function importHistory(
  characterName: string,
  entries: BackendHistoryEntry[],
): Promise<boolean> {
  const messages: ImportMessage[] = []
  for (const entry of entries) {
    const text = (entry.content ?? '').trim()
    if (!text) continue
    if (text.startsWith(SUMMARY_PREFIX)) continue // 摘要不是原始对话
    // 历史图片不重传（服务端也不会把历史图塞进 prompt），只留一个文字占位
    messages.push({
      side: entry.role === 'user' ? 'mine' : 'other',
      text: entry.image ? '[图片]' : text,
    })
  }
  if (messages.length === 0) return false

  const packageJson = {
    version: 1,
    myGender: 'male',
    cards: [
      {
        members: [],
        conversations: [{ name: characterName, messages }],
      },
    ],
  }
  const blob = new Blob([JSON.stringify(packageJson)], { type: 'application/json' })
  const form = new FormData()
  form.append('file', blob, 'baker-history.json')
  form.append('mode', 'merge')

  try {
    const response = await apiFetch('/imports', { method: 'POST', body: form })
    if (!response.ok) {
      devWarn('[backend] 历史导入失败，退化为新建会话:', await readError(response))
      return false
    }
    const result = (await response.json()) as { conversations?: number; duplicate?: boolean }
    devInfo('[backend] 历史导入完成', result)
    return (result.conversations ?? 0) > 0
  } catch (err) {
    devWarn('[backend] 历史导入异常，退化为新建会话:', err)
    return false
  }
}

/**
 * 确保这个角色有一条服务端会话
 *
 * 顺序：本地绑定 → 服务端已有会话 → 导入历史 → 新建。
 * 空会话（next_seq <= 1）会被跳过：那种会话没有任何上下文，不如导入一份。
 */
async function ensureConversation(
  characterId: string,
  characterName: string,
  seed: BackendHistoryEntry[],
  localId?: string,
): Promise<Binding> {
  const key = bindingKey(characterId, localId)
  const bound = readBindings()[key]
  if (bound?.id) return bound

  const existingLock = bindingLocks.get(key)
  if (existingLock) return existingLock

  const task = (async (): Promise<Binding> => {
    // ---- 老绑定一次性认领 ----
    // 升级前的绑定只按角色名记（`charId → 会话`）。第一次在某个存档里发消息时，
    // 把它认领给**这段**对话，历史才不会"像是丢了"；认领完就删掉旧键，
    // 于是同一角色的其它存档不会再抢到它（那正是"串"的来源）。
    if (localId) {
      const map = readBindings()
      const legacy = map[characterId]
      if (legacy?.id) {
        delete map[characterId]
        map[key] = legacy
        writeBindings(map)
        devInfo('[backend] 老绑定认领给这段对话', characterId, localId)
        return legacy
      }
    }

    // 有 localId 就**绝不按角色去猜**服务端上的会话 —— 一猜就会串到别的存档。
    // 只有老调用点（没给 localId）才沿用"取该角色最近活跃那条"。
    let conversation: ConversationOut | null = localId
      ? null
      : await findConversation(characterId)

    // 空会话（next_seq 还是 0）：删掉它，走"导入历史"那条路（导入会新建一条带历史的）。
    // 注意新会话的 next_seq 是 **0**（第一条消息才拿 seq=1），不是 1。
    if (conversation && (conversation.next_seq ?? 0) === 0 && seed.length > 0) {
      const removed = await deleteConversation(conversation.id)
      if (removed) conversation = null
    }

    if (!conversation && seed.length > 0) {
      // ⚠️ 导入之后**不能**再用"该角色最近活跃的那条"去认领 ——
      // 同角色有两条会话时，next_seq 可能撞平，排序会稳定地选回最早那条，
      // 于是第二段存档被绑到第一段的会话上（就是用户看到的"串"）。
      // 正确做法：导入前后各拉一次列表，取"这次新冒出来的"那条。
      const before = localId ? new Set((await listConversations()).map((c) => c.id)) : null
      const imported = await importHistory(characterName, seed)
      if (imported) {
        if (before) {
          const fresh = (await listConversations())
            .filter((c) => !before.has(c.id))
            .sort((a, b) => (b.next_seq ?? 0) - (a.next_seq ?? 0))
          conversation = fresh[0] ?? null
        } else {
          conversation = await findConversation(characterId)
        }
      }
    }

    if (!conversation) conversation = await createConversation(characterId, characterName)

    const binding: Binding = { id: conversation.id, at: Date.now() }
    const map = readBindings()
    map[key] = binding
    writeBindings(map)
    return binding
  })()

  bindingLocks.set(key, task)
  try {
    return await task
  } finally {
    bindingLocks.delete(key)
  }
}

/** 删除服务端会话（404 视为已删） */
async function deleteConversation(conversationId: string): Promise<boolean> {
  try {
    const response = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
      method: 'DELETE',
    })
    return response.ok || response.status === 404
  } catch {
    return false
  }
}

// ---------------------------------------------------------------------------
// 幂等键：一轮对话在"流式 → 非流式回退"之间必须用同一个 id
// ---------------------------------------------------------------------------

interface PendingTurn {
  clientMsgId: string
  at: number
}

const pendingTurns = new Map<string, PendingTurn>()

/** 一轮最多复用同一个幂等键多久（毫秒）；过期视为新的一轮 */
const PENDING_TTL = 10 * 60 * 1000

function turnKey(conversationId: string, text: string, historyLength: number): string {
  return `${conversationId}|${historyLength}|${text.slice(0, 80)}`
}

function clientMsgIdFor(key: string): string {
  const now = Date.now()
  const existing = pendingTurns.get(key)
  if (existing && now - existing.at < PENDING_TTL) {
    existing.at = now
    return existing.clientMsgId
  }
  const id = `c-${now.toString(36)}-${Math.random().toString(36).slice(2, 8)}`
  pendingTurns.set(key, { clientMsgId: id, at: now })
  return id
}

function clearPendingTurn(key: string): void {
  pendingTurns.delete(key)
}

// ---------------------------------------------------------------------------
// 当前轮的准备：图片上传 / 消息体
// ---------------------------------------------------------------------------

/** 本轮附带的图片（dataURL） */
function currentImageOf(request: BackendRequest): string {
  const last = request.history[request.history.length - 1]
  const dataUrl = last?.image ?? ''
  return dataUrl.startsWith('data:image/') ? dataUrl : ''
}

/**
 * 上传一张 dataURL 图片，返回附件 id
 *
 * 新后端只收图片、限 6 MB、按设备去重；取回要鉴权（`attachment_url` 由服务端给）。
 * 失败返回 null：这一轮退化成纯文字，不让整条消息发不出去。
 */
async function uploadImage(dataUrl: string): Promise<string | null> {
  try {
    const blob = await (await fetch(dataUrl)).blob()
    const mime = blob.type || 'image/png'
    const ext = mime.includes('jpeg') ? 'jpg' : mime.includes('webp') ? 'webp' : 'png'
    const form = new FormData()
    form.append('file', blob, `image.${ext}`)
    const response = await apiFetch('/attachments', { method: 'POST', body: form })
    if (!response.ok) {
      devWarn('[backend] 图片上传失败:', await readError(response))
      return null
    }
    const data = (await response.json()) as { id?: string }
    return data.id ?? null
  } catch (err) {
    devWarn('[backend] 图片上传异常:', err)
    return null
  }
}

/** 本地时间（只进提示词，不入库）：`2026-11-17 23:20 周一` */
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

/** 服务端 usage → 旧接口 usage */
function toUsage(usage?: {
  prompt_tokens?: number
  completion_tokens?: number
  cached_tokens?: number
  total_ms?: number
} | null): BackendUsage | undefined {
  if (!usage) return undefined
  const prompt = usage.prompt_tokens ?? 0
  const completion = usage.completion_tokens ?? 0
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: prompt + completion,
    cached_tokens: usage.cached_tokens ?? 0,
    total_ms: usage.total_ms ?? 0,
  }
}

/** 写消息的响应形状（只取用到的字段） */
interface MessageCreated {
  seq: number
  duplicate: boolean
  reply: string | null
  reply_seq: number | null
  speaker?: string
  silent?: boolean
  usage?: {
    prompt_tokens?: number
    completion_tokens?: number
    cached_tokens?: number
    total_ms?: number
  } | null
}

/** 一条服务端历史消息 */
interface MessageOut {
  seq: number
  side: string
  speaker: string
  text: string
  attachment_id?: string | null
  attachment_url?: string | null
}

/**
 * 读"这一轮之后"的助手回复
 *
 * 用途：请求其实成功了、但响应在网络上丢了（或流式断在半路）—— 重发同一个
 * client_msg_id 会得到 duplicate，此时用 seq 把已经存下的回复捞出来，
 * 既不重复生成，也不给玩家弹一条空回复。
 */
async function findReplyAfter(conversationId: string, seq: number): Promise<string | null> {
  try {
    const response = await apiFetch(
      `/conversations/${encodeURIComponent(conversationId)}/messages` +
        `?after_seq=${seq}&limit=20`,
      { method: 'GET' },
    )
    if (!response.ok) return null
    const data = (await response.json()) as { items?: MessageOut[] }
    const hit = (data.items ?? []).find((item) => item.side === 'assistant')
    return hit?.text ?? null
  } catch {
    return null
  }
}

/** 「继续说」：没有新的用户消息，让角色接着上一条写 */
async function continueReply(
  conversationId: string,
  clientMsgId: string,
  think: boolean,
): Promise<{ reply: string; usage?: BackendUsage }> {
  const response = await apiFetch(
    `/conversations/${encodeURIComponent(conversationId)}/continue`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        client_msg_id: clientMsgId,
        // 关掉时间感知：不发时间（后端也会据 time_enabled 丢掉）
        local_time: readTimeAware() ? formatLocalTime() : '',
        time_enabled: readTimeAware(),
        think,
      }),
    },
  )
  if (!response.ok) throw await readError(response)
  const data = (await response.json()) as { reply?: string; usage?: MessageCreated['usage'] }
  return { reply: data.reply ?? '', usage: toUsage(data.usage) }
}

// ---------------------------------------------------------------------------
// 主入口：非流式
// ---------------------------------------------------------------------------

/** 这一轮准备好的上下文 */
interface PreparedTurn {
  characterId: string
  characterName: string
  conversationId: string
  /** 幂等键（跨流式/非流式回退稳定） */
  clientMsgId: string
  /** 幂等键在 pendingTurns 里的原始 key（成功后要按它清） */
  turnKey: string
  /** 第一次绑定该角色时要导入的历史（会话失效重建时还要用） */
  seed: BackendHistoryEntry[]
  /**
   * 这段对话的稳定绑定键（`Conversation.localId` 或 `r:<服务端会话 id>`）
   *
   * ⚠️ 重建绑定时必须一起带上：2026-11-19 之前这里漏了它，
   * 于是一旦遇到"会话不存在 → 重建绑定"，就会按角色重新猜一条会话 ——
   * 同角色的另一段对话被猜中，就是用户看到的"串"。
   */
  conversationKey: string
  /** 发给服务端的正文（图片轮可能是空串） */
  text: string
  attachmentId: string | null
  /** 是否需要走「继续说」（没有新用户消息） */
  continuing: boolean
  immersive: boolean
  think: boolean
}

/**
 * 准备一轮对话：设备 → 会话 → 历史导入 → 图片上传
 *
 * 所有网络动作都在这里，主入口只负责"发消息 + 解析结果"。
 */
async function prepareTurn(
  request: BackendRequest,
  signal?: AbortSignal,
): Promise<PreparedTurn> {
  syncCharactersOnce()
  await ensureDeviceToken(signal)

  const characterName = request.character || ''
  const characterId = request.character_id || CHARACTER_ID_MAP[characterName] || ''
  if (!characterId) {
    throw new ApiError(
      400,
      'no_character',
      `没有收录「${characterName || '这个角色'}」，请先选择一位干员`,
    )
  }

  // 当前轮图片：约定放在 history 最后一条（useAiChat 的图片发送就是这样构造的）
  const imageDataUrl = currentImageOf(request)
  const seed = imageDataUrl ? request.history.slice(0, -1) : request.history

  const binding = await ensureConversation(
    characterId,
    characterName,
    seed,
    request.conversation_key,
  )

  const attachmentId = imageDataUrl ? await uploadImage(imageDataUrl) : null
  const text = (request.message ?? '').trim()
  const continuing = !text && !attachmentId
  const key = turnKey(binding.id, text, request.history.length)

  return {
    characterId,
    characterName,
    conversationId: binding.id,
    conversationKey: request.conversation_key ?? '',
    clientMsgId: clientMsgIdFor(key),
    turnKey: key,
    seed,
    // 图片轮的正文通常是占位符「[图片]」，保持原样发过去
    text: text || (attachmentId ? '[图片]' : ''),
    attachmentId,
    continuing,
    immersive: request.immersive_mode ?? true,
    think: request.think ?? false,
  }
}

/** 一轮的最终回复 */
interface TurnReply {
  reply: string
  usage?: BackendUsage
  /** 服务端认为这一轮"沉默"（群聊主持人判定 END；单聊不会出现） */
  silent: boolean
}

/** 发一条消息并等回复；内部处理重试 / 幂等 / 会话失效重建 */
async function sendTurn(prepared: PreparedTurn, signal?: AbortSignal): Promise<TurnReply> {
  const MAX_ATTEMPTS = 3

  if (prepared.continuing) {
    const result = await continueReply(
      prepared.conversationId,
      prepared.clientMsgId,
      prepared.think,
    )
    return { reply: result.reply, usage: result.usage, silent: false }
  }

  let lastError: unknown = null
  let attempt = 0
  while (attempt < MAX_ATTEMPTS) {
    attempt += 1
    try {
      const response = await apiFetch(
        `/conversations/${encodeURIComponent(prepared.conversationId)}/messages`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            client_msg_id: prepared.clientMsgId,
            text: prepared.text,
            attachment_id: prepared.attachmentId,
            speaker: '',
            generate: true,
            immersive: prepared.immersive,
            think: prepared.think,
            local_time: readTimeAware() ? formatLocalTime() : '',
            time_enabled: readTimeAware(),
          }),
          signal,
        },
      )

      if (!response.ok) {
        const error = await readError(response)
        // 会话没了（服务端重置 / 登出后归属变化）：丢掉绑定重建一次
        if (error.code === 'no_conversation') {
          devWarn('[backend] 会话不存在，重建绑定后重试')
          dropBinding(prepared.characterId)
          const rebuilt = await ensureConversation(
            prepared.characterId,
            prepared.characterName,
            prepared.seed,
            prepared.conversationKey || undefined,
          )
          prepared.conversationId = rebuilt.id
          prepared.turnKey = turnKey(rebuilt.id, prepared.text, prepared.seed.length)
          prepared.clientMsgId = clientMsgIdFor(prepared.turnKey)
          continue
        }
        if (error.retryable && attempt < MAX_ATTEMPTS) {
          lastError = error
          await sleep(backoffMs(error, attempt))
          continue
        }
        throw error
      }

      const data = (await response.json()) as MessageCreated
      if (data.duplicate) {
        // 这条 client_msg_id 之前存过了：把已经生成的回复捞回来
        const existing = await findReplyAfter(prepared.conversationId, data.seq)
        if (existing !== null) {
          return { reply: existing, usage: toUsage(data.usage), silent: false }
        }
        // 存过用户消息但没有回复（例如上次 429 之后）：用「继续说」补一条
        try {
          const continued = await continueReply(
            prepared.conversationId,
            `${prepared.clientMsgId}-cont`,
            prepared.think,
          )
          return { reply: continued.reply, usage: continued.usage, silent: false }
        } catch (err) {
          throw err instanceof ApiError
            ? err
            : new ApiError(0, 'no_reply', '消息已保存，但生成失败，请稍后点「重新生成」')
        }
      }

      return {
        reply: data.reply ?? '',
        usage: toUsage(data.usage),
        silent: data.silent ?? false,
      }
    } catch (err) {
      lastError = err
      const aborted = err instanceof DOMException && err.name === 'AbortError'
      if (aborted) throw err
      const retryable =
        err instanceof ApiError
          ? err.retryable || err.status === 0
          : err instanceof TypeError
      if (!retryable || attempt >= MAX_ATTEMPTS) break
      await sleep(backoffMs(err instanceof ApiError ? err : null, attempt))
    }
  }

  throw lastError instanceof Error ? lastError : new Error('请求失败')
}

function backoffMs(error: ApiError | null, attempt: number): number {
  if (error?.retryAfter) {
    return Math.min(error.retryAfter, 30) * 1000
  }
  return 700 * attempt
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * 请求一次回复（非流式）
 *
 * 这是 useAiChat 在"后端模式"下唯一的入口：给 message + history，
 * 拿回一段完整回复。
 */
export async function fetchBackendReply(
  _config: ApiConfig,
  request: BackendRequest,
  signal?: AbortSignal,
): Promise<BackendReplyResult> {
  const prepared = await prepareTurn(request, signal)
  const reply = await sendTurn(prepared, signal)

  // 成功后清掉幂等键；失败时保留，重试（回退 / 重新生成）时还能复用同一条消息
  clearPendingTurn(prepared.turnKey)

  if (reply.silent) {
    devInfo('[backend] 服务端判定本轮沉默（群聊 END），单聊不应出现')
  }

  return {
    reply: reply.reply,
    // 新后端把表情翻译成「（微笑）」这类文字，不再单独下发表情 token
    mood: undefined,
    usage: reply.usage,
    knowledgeSource: undefined,
  }
}

// ---------------------------------------------------------------------------
// 主入口：流式（SSE）
// ---------------------------------------------------------------------------

/** SSE 帧解析状态 */
interface StreamParseState {
  receivedDelta: boolean
  accumulated: string
  replySeq: number | null
}

/**
 * 请求一次回复（SSE 流式）
 *
 * 新后端的帧只有三种：
 *   event: delta  data: {"text":"…"}
 *   event: done   data: {"seq":12,"reply_seq":13}
 *   event: error  data: {"code":"…","message":"…"}
 *
 * 失败语义（与 useAiChat 的回退策略一一对应）：
 *   - 收到任何 delta **之前**链路失败 → BackendStreamUnavailableError（调用方回退非流式）
 *   - 已经出过字之后失败 → 原样抛出（不重发，避免重复）
 *   - 服务端 error 帧 → 原样抛出（明确报错，不是链路问题）
 */
export async function fetchBackendReplyStream(
  _config: ApiConfig,
  request: BackendRequest,
  options: BackendStreamOptions = {},
): Promise<BackendReplyResult> {
  const { onDelta, signal } = options
  const prepared = await prepareTurn(request, signal)

  // "继续说"没有专门的流式端点（后端只在 /messages:stream 做流）
  if (prepared.continuing) {
    const result = await continueReply(prepared.conversationId, prepared.clientMsgId, prepared.think)
    if (result.reply) onDelta?.(result.reply)
    clearPendingTurn(prepared.turnKey)
    return { reply: result.reply, usage: result.usage }
  }

  const url = v1Url(
    `/conversations/${encodeURIComponent(prepared.conversationId)}/messages:stream`,
  )

  let response: Response
  try {
    const token = await ensureDeviceToken(signal)
    response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        client_msg_id: prepared.clientMsgId,
        text: prepared.text,
        attachment_id: prepared.attachmentId,
        speaker: '',
        generate: true,
        immersive: prepared.immersive,
        think: prepared.think,
        local_time: readTimeAware() ? formatLocalTime() : '',
        time_enabled: readTimeAware(),
      }),
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new BackendStreamUnavailableError(
      err instanceof TypeError
        ? '网络连接失败：无法连接到服务器，请检查网络后重试'
        : `后端流式请求失败：${err instanceof Error ? err.message : String(err)}`,
    )
  }

  // 409：这条消息之前存过了（例如上一次流式断在半路）→ 直接把已存回复当作结果
  if (response.status === 409) {
    const error = await readError(response)
    const seq = typeof error.details.seq === 'number' ? error.details.seq : 0
    const fallback = await findReplyAfter(prepared.conversationId, seq)
    if (fallback !== null) {
      onDelta?.(fallback)
      clearPendingTurn(prepared.turnKey)
      return { reply: fallback }
    }
    throw new BackendStreamUnavailableError('消息已存在但没有找到回复，改用非流式重试')
  }

  if (!response.ok) {
    const error = await readError(response)
    // 明确报错（限速 / 上游失败）：不回退，避免"回退=重发=再占一次额度"
    throw error
  }

  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream') || !response.body) {
    throw new BackendStreamUnavailableError(
      `后端流式响应不是 SSE（Content-Type: ${contentType || '未知'}）`,
    )
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let frameEvent = ''
  let frameData: string[] = []
  const state: StreamParseState = { receivedDelta: false, accumulated: '', replySeq: null }

  const handleFrame = (event: string, data: string): void => {
    if (!data) return
    let payload: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(data)
      if (!parsed || typeof parsed !== 'object') return
      payload = parsed as Record<string, unknown>
    } catch {
      return
    }

    if (event === 'delta') {
      const text = payload.text
      if (typeof text === 'string' && text) {
        state.receivedDelta = true
        state.accumulated += text
        onDelta?.(text)
      }
      return
    }
    if (event === 'done') {
      if (typeof payload.reply_seq === 'number') state.replySeq = payload.reply_seq
      return
    }
    if (event === 'error') {
      const message =
        typeof payload.message === 'string' && payload.message
          ? payload.message
          : '后端流式返回错误'
      throw new ApiError(502, typeof payload.code === 'string' ? payload.code : 'stream_error', message)
    }
    // 未知事件忽略（后端将来新增事件不该打断本次回复）
  }

  const flushFrame = (): void => {
    const event = frameEvent
    const data = frameData.join('\n')
    frameEvent = ''
    frameData = []
    if (!data && !event) return
    handleFrame(event, data)
  }

  const consumeLine = (raw: string): void => {
    const line = raw.endsWith('\r') ? raw.slice(0, -1) : raw
    if (line === '') {
      flushFrame()
      return
    }
    if (line.startsWith(':')) return // 心跳注释
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') frameEvent = value
    else if (field === 'data') frameData.push(value)
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) consumeLine(line)
    }
    if (buffer !== '') consumeLine(buffer)
    flushFrame()
  } catch (err) {
    reader.cancel().catch(() => {})
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    if (err instanceof ApiError) throw err
    if (state.receivedDelta) throw err instanceof Error ? err : new Error(String(err))
    throw new BackendStreamUnavailableError(
      err instanceof Error ? err.message : String(err),
    )
  }

  clearPendingTurn(prepared.turnKey)

  return { reply: state.accumulated, usage: undefined }
}

// ---------------------------------------------------------------------------
// 输入推荐
// ---------------------------------------------------------------------------

/**
 * 拉取「我接下来可以说什么」的候选
 *
 * 新后端是会话级接口：`POST /v1/conversations/{id}/suggestions`（无请求体）。
 * 失败一律抛错，由调用方降级为"暂无推荐"。
 */
export async function fetchBackendSuggestions(
  input: BackendSuggestionInput,
  signal?: AbortSignal,
): Promise<string[]> {
  syncCharactersOnce()
  const characterName = input.character || ''
  const characterId = input.characterId || CHARACTER_ID_MAP[characterName] || ''
  if (!characterId) return []

  const seed: BackendHistoryEntry[] = input.history.slice(-BACKEND_HISTORY_LIMIT).map((h) => ({
    role: h.side === 'mine' ? 'user' : 'assistant',
    content: h.image ? '[图片]' : h.text,
  }))

  const binding = await ensureConversation(
    characterId,
    characterName,
    seed,
    input.conversationKey,
  )
  const response = await apiFetch(
    `/conversations/${encodeURIComponent(binding.id)}/suggestions`,
    { method: 'POST', signal },
  )
  if (!response.ok) {
    const error = await readError(response)
    // 会话被删（服务端重置）：重建后放弃这一次推荐（推荐不值得多打一次上游）
    if (error.code === 'no_conversation') dropBinding(characterId)
    throw error
  }
  const data = (await response.json()) as { suggestions?: unknown }
  if (!Array.isArray(data.suggestions)) return []
  return data.suggestions.filter(
    (item): item is string => typeof item === 'string' && item.trim() !== '',
  )
}

// ---------------------------------------------------------------------------
// 连接测试（设置弹窗）
// ---------------------------------------------------------------------------

/** 测试后端连接：`GET /v1/meta` 2xx 即视为连通 */
export async function testBackendConnection(
  _config: ApiConfig,
  _character = '',
): Promise<{ ok: boolean; message: string }> {
  try {
    const response = await apiFetch('/meta', { method: 'GET' })
    if (!response.ok) {
      const error = await readError(response)
      return { ok: false, message: `连接失败 (${response.status}): ${error.message}` }
    }
    const meta = (await response.json()) as {
      product?: string
      api?: string
      capabilities?: Record<string, boolean>
    }
    const caps = meta.capabilities ?? {}
    const extra = [
      `接口 ${meta.api ?? '?'}`,
      `web_search=${caps.web_search ? 'true' : 'false'}`,
    ].join(' · ')
    return { ok: true, message: `连接成功（${meta.product ?? 'BakerAI'} · ${extra}）` }
  } catch (err) {
    return {
      ok: false,
      message: `连接失败: ${err instanceof Error ? err.message : String(err)}`,
    }
  }
}
