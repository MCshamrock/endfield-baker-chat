// =============================================================================
// AI 聊天编排 composable(useAiChat)
// -----------------------------------------------------------------------------
// 连接 store(chat) + settings + llm,编排完整的 AI 聊天流程:
//   1. 用户发送消息 → store.sendUserMessage
//   2. 构建 LLM 消息(系统提示词 + 角色提示词 + 历史消息)
//   3. 创建 AI 占位消息 → store.beginAiResponse(loading 动画)
//   4. 流式调用 API,缓冲完整回复(loading 动画持续)
//   5. 回复完成 → 按 \n 分段,逐段顺序显示:
//      - 第一段填入当前 loading 气泡 → finishAiSegment
//      - 后续段:短暂假 loading → 新气泡显示该段 → finishAiSegment
//      - 最后一段:finishAiResponse(结束整体响应)
//
// 【响应上下文】beginAiResponse 会返回本轮独立的 ctx(锁定目标会话 + 说话人),
// 后续所有写入都必须显式传入它。这是修复"串台"的关键:
//   - 等待回复期间切换会话 / 多会话并发请求,回复仍只写回它自己发起的会话
//   - 头像与角色名取自 ctx,不会被其他会话的请求覆盖
//   - 用 isCtxActive(ctx) 判断本轮是否已失效(用户点停止,或同会话又发起了
//     新请求):失效后迟到的内容一律丢弃,不污染新请求
//   - ctx 还锁定了**会话对象本身**:删/建/导入会让同一个下标记上另一段会话,
//     那时写入同样作废(下标只是位置,不是身份)
//
// 【目标会话锁定】每个入口都在第一个 await 之前抓 { sub, conv } 并一路透传
// (见 AiTurnTarget):人设、历史、写入位置必须来自同一段会话 —— 中间隔着
// 网络(智能总结最长几十秒),等 await 回来再读 activeSub 就晚了。
// =============================================================================

import { useChatStore } from '../stores/chat'
import { useSettingsStore } from '../stores/settings'
import { streamChat, buildMessages } from '../utils/llm'
import {
  buildBackendRequest,
  fetchBackendReply,
  fetchBackendReplyStream,
  fetchBackendSuggestions,
  BackendStreamUnavailableError,
  CHARACTER_ID_MAP,
} from '../utils/backend'
import { applyLengthPolicy, buildEmphasisRule } from '../constants/prompts'
import {
  splitAiSegments,
  splitAiSegmentsStreaming,
} from '../utils/aiText'
import { stripImmersive, ImmersiveFilter } from '../utils/immersive'
import type { Conversation } from '../types/chat'
import { conversationKeyOf } from '../utils/localConversation'
import {
  customChatCompletion,
  parseSuggestionList,
  SUGGEST_SYSTEM_SINGLE,
} from '../utils/customApi'
import {
  requestCustomSummary,
  resolveCustomSummaryContext,
  CUSTOM_SUMMARY_TIMEOUT_MS,
} from '../utils/customSummary'
import {
  isSummaryEntry,
  planRoundTrim,
  TRIM_MIN_KEEP_ROUNDS,
} from '../utils/summaryTrim'
import {
  pickRealPromptTokens,
  reachesSummaryThreshold,
  resolvePromptOccupancy,
} from '../utils/promptUsage'
import { useUsageStore } from '../stores/usage'
import { useContextCompaction } from './useContextCompaction'
import { useReplyReveal } from './useReplyReveal'
import {
  estimateEntries,
  estimateEntry,
  estimateMessages,
  estimateText,
} from '../utils/tokenEstimate'
import {
  BACKEND_PROMPT_TOKENS,
  recordMeasuredPromptTokens,
} from '../constants/summaryBudget'
import {
  clampSummaryTriggerPercent,
  computeSummaryTriggerBounds,
  summaryTriggerRatio,
} from '../utils/summaryThreshold'
import { summaryPromptTokens } from '../utils/summaryPromptBudget'
import type { NormalizedUsage } from '../utils/usage'
import { devWarn } from '../utils/logger'

// 【2026-11-18 P0】旧版直连的写死窗口 / 提示词常量（512000 / 3000）随
// applySmartSummary 一起删除；现在只剩 constants/summaryBudget.ts 里那组。

/**
 * 从后端返回的 usage 里取"本轮**输出** token"
 *
 * 为什么只累计输出 token：对话历史里逐轮累积增长的就是模型生成的部分；
 * 输入 token 每轮都会把整段历史重新算一遍，累加它无法反映历史长度。
 *
 * 后端统一返回 { prompt_tokens, completion_tokens, total_tokens, model, api_id }。
 */
function pickOutputTokens(usage?: BackendUsageLike): number {
  if (!usage || typeof usage !== 'object') return 0
  const c = Number(usage.completion_tokens)
  return Number.isFinite(c) && c > 0 ? c : 0
}

/** 后端 usage 的最小形状（避免在此处引入 backend.ts 的类型耦合） */
interface BackendUsageLike {
  prompt_tokens?: number
  completion_tokens?: number
  total_tokens?: number
}

/** 聊天历史条目(与 chat store 的 contextHistory 形状一致) */
type ChatHistoryEntry = { side: 'other' | 'mine'; text: string; image?: string }

/**
 * 默认模式输入:当前消息 + 发送前截取的历史(不含当前输入)
 *
 * 【2026-11-18 P0】原来的 needSummary / contextWindowTokens 两个字段已删除：
 * 服务端每轮自己压缩，前端不需要（也不会）再请求总结。
 */
interface BackendInput {
  message: string
  history: ChatHistoryEntry[]
  /** 「重新生成」：服务端重写这一轮的回话，不新增玩家消息（见 utils/backend.ts） */
  regenerate?: boolean
}

/**
 * 一轮 AI 回复**锁定的目标会话**
 *
 * 为什么要对象而不是下标:下标只是"扁平数组里的位置"。从读取人设/历史到真正
 * 把回复写回会话之间隔着网络 await(智能总结最长可能等几十秒),期间玩家完全
 * 可能切走、甚至删掉/新建会话 —— 那时再读 `activeSub`,拿到的是另一段会话
 * (A 的人设配 B 的历史,回复写进 B)。因此每个入口都在**第一个 await 之前**
 * 把 { 下标, 会话对象 } 一起抓下来,后面所有读写都显式带上它。
 *
 * 会话对象用于归属校验:写入前确认"这个下标记的还是不是同一段会话"。
 */
interface AiTurnTarget {
  sub: number
  conv: Conversation
}

/** 延迟工具(ms):分段显示模拟"对方正在输入"的节奏 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 【2026-11-18 P0】原来的 applySmartSummary()（旧版直连模式的前端智能总结：
// 按 512K 窗口算 80% 预算 → 调外部总结 API → 摘要插到历史最前面）已整段删除。
// 原因：它只服务 apiMode === 'legacy'，而旧版直连模式已经在本次下线
// （它依赖的 `/api/legacy-key` 新后端根本没有）。
// 现在前端只剩一种总结：自定义 API 模式的 maybeCustomSummary()（下面这个）。

/**
 * 自定义 API 模式：本轮是否需要"前端总结 + 整轮裁剪"（2026-11-14 新增）
 *
 * 判据（与后端模式同一套，但窗口来自自定义模式自己的上下文）：
 *   上一轮真实 usage.prompt_tokens（没有则"写死提示词 + 历史估算"）
 *   ≥ 自定义上下文窗口 × 用户设定阈值（45~80%）
 *
 * 命中后前端直接调用用户配置的 OpenAI 兼容端点生成总结（不经过后端），
 * 成功后 contextHistory = [总结, ...按整轮裁剪后的历史]；失败/20s 超时则
 * 只裁剪、不插总结，并挂一条"总结失败，已跳过"提示，绝不阻断本轮聊天。
 *
 * @returns 是否真的执行了一次总结流程（attempted）以及是否拿到总结（summarized）
 */
async function maybeCustomSummary(
  history: ChatHistoryEntry[],
  settingsStore: ReturnType<typeof useSettingsStore>,
  chatStore: ReturnType<typeof useChatStore>,
  targetSub?: number,
): Promise<{ attempted: boolean; summarized: boolean }> {
  const idle = { attempted: false, summarized: false }
  if (!settingsStore.summaryConfig.enabled) return idle
  if (settingsStore.apiConfig.apiMode !== 'custom') return idle
  if (history.length === 0) return idle

  const sub = targetSub ?? chatStore.activeSub
  if (sub === null) return idle
  const conv = chatStore.conversations[sub]
  if (!conv) return idle

  const config = settingsStore.apiConfig
  if (!config.baseUrl || !config.apiKey || !config.model) return idle

  // 上下文窗口：apiConfig.contextWindow → 模型库 → 32768
  const context = await resolveCustomSummaryContext(config)
  // 自定义模式的提示词占用 = 实测校准值 与 当前两条 system 消息估算 的较大值，
  // 因此用户改了世界观 / 角色提示词后，这个值（以及可调上限）会实时变化。
  const promptTokens = summaryPromptTokens(
    'custom',
    settingsStore.getSystemMessage(),
    settingsStore.getCharacterMessage(conv.name),
  )
  const fallback = promptTokens + estimateEntries(history)
  const occupancy = resolvePromptOccupancy(chatStore.getLastPromptTokens(sub), fallback)

  // 触发阈值读用户设置（45~80），并按"提示词占比 + 10% 回复冗余"夹到可调范围。
  const bounds = computeSummaryTriggerBounds(promptTokens, context.contextWindow)
  const triggerPercent = clampSummaryTriggerPercent(
    settingsStore.summaryConfig.triggerPercent,
    bounds,
  )
  if (triggerPercent !== settingsStore.summaryConfig.triggerPercent) {
    settingsStore.updateSummaryConfig({ triggerPercent })
  }
  if (!reachesSummaryThreshold(occupancy, context.contextWindow, summaryTriggerRatio(triggerPercent))) {
    return idle
  }

  // 旧总结头由新总结取代：不参与本轮轮次裁剪，避免请求里出现两条总结
  const dialogue = isSummaryEntry(history[0]) ? history.slice(1) : history
  const plan = planRoundTrim(dialogue, {
    contextWindow: context.contextWindow,
    minKeepRounds: TRIM_MIN_KEEP_ROUNDS,
    fixedTokens: promptTokens,
  })
  // dropped 以"裁剪前的完整历史"为准（含被新总结取代的旧总结头）
  const dropped = history.length - plan.kept.length

  chatStore.beginSummaryWait(sub, CUSTOM_SUMMARY_TIMEOUT_MS)
  try {
    // 注意：总结输入用**完整历史**（含旧总结），输出取代旧总结
    const result = await requestCustomSummary(config, history, context.contextWindow)
    // 总结请求本身也消耗了真实 prompt token —— 计入本会话累计值
    if (result.promptTokens > 0) chatStore.addPromptTokens(result.promptTokens, sub)
    chatStore.applyCustomSummary(sub, result.text, plan.kept, dropped)
    return { attempted: true, summarized: true }
  } catch (err) {
    devWarn('[summary] 自定义 API 总结失败，已跳过:', err)
    // 失败也要裁剪：否则下一轮仍会拿超预算历史去请求。
    // 注意保留旧总结头 —— 新总结没生成出来，旧记忆不能一起丢。
    const keptWithOldSummary = isSummaryEntry(history[0])
      ? [history[0], ...plan.kept]
      : plan.kept
    chatStore.applyHistoryTrim(
      sub,
      keptWithOldSummary,
      history.length - keptWithOldSummary.length,
    )
    chatStore.noteSummaryFailure(sub)
    return { attempted: true, summarized: false }
  } finally {
    chatStore.endSummaryWait(sub)
  }
}

/**
 * 按当前模式决定"要传给 triggerAiResponse 的已裁剪历史"
 *
 * @returns 已裁剪的历史;返回 **undefined** 表示不接管 ——
 *          由 triggerAiResponse 在上下文压缩之后再从 store 现取一次。
 *
 * 为什么自定义 API 模式必须返回 undefined(而不是"原样返回一份完整历史"):
 *   压缩会改写 contextHistory,而压缩发生在 triggerAiResponse **内部**。
 *   若在这里就把历史快照取好传进去,压缩的结果永远用不上 ——
 *   请求里发出去的仍是压缩前那一份,历史会一路涨到超预算被服务端拒绝。
 *
 * 自定义模式在这里先跑一次"前端总结 + 整轮裁剪"(见 maybeCustomSummary)：
 *   它直接改写 store 的 contextHistory，所以仍然返回 undefined ——
 *   让 triggerAiResponse 去 store 现取刚裁好的那一份。
 */
async function planHistory(
  history: ChatHistoryEntry[],
  settingsStore: ReturnType<typeof useSettingsStore>,
  chatStore: ReturnType<typeof useChatStore>,
  /** 目标会话下标(缺省 = 当前会话) */
  targetSub?: number,
): Promise<ChatHistoryEntry[] | undefined> {
  if (settingsStore.apiConfig.apiMode === 'custom') {
    await maybeCustomSummary(history, settingsStore, chatStore, targetSub)
    return undefined
  }
  // 默认模式（backend）：前端不做任何总结/裁剪 —— 历史原样交给
  // buildBackendRequest（内部截最近 30 条），压缩由服务端每轮自己做（docs/08）。
  return undefined
}

// 【2026-11-18 P0】原来的 shouldRequestBackendSummary() 已删除：
// 它按 131072 窗口 × 45~80% 判"该总结了"，然后让请求带 need_summary=true。
// 新后端忽略这个字段（自己按 200k/80%/压到 50% 压），所以判据永远等不到结果，
// 只会让用户白等一拍 + 本地把历史裁掉。现在没有任何调用者。

/** 群聊里"让某个指定角色发言"时的说话人覆盖 */
export interface SpeakerOverride {
  name: string
  avatar: string
}

/**
 * 计算固定前缀指纹(djb2 变体)
 *
 * 用于检测"世界观 / 角色提示词被改动"——前缀一变,服务端缓存必然全部失效,
 * 面板据此显示提示而非让用户误以为是缓存没生效。
 */
function hashText(text: string): string {
  let h = 5381
  for (let i = 0; i < text.length; i++) {
    h = ((h << 5) + h + text.charCodeAt(i)) >>> 0
  }
  return h.toString(36)
}

/**
 * custom 模式的单聊「AI 推荐回复」(前端本地实现,不走后端网关)
 *
 * 【审计修复 2026-10-04】此前 useAiChat.fetchSuggestions 无论什么模式都调用
 * fetchBackendSuggestions(网关 /chat/suggestions)—— 在 apiMode === 'custom'
 * 时这属于"偷偷走后端"。自定义模式必须全程只用用户自己的 API,
 * 因此这里用 customApi 直连实现同一功能。
 *
 * 提示词口径与后端 prompt_core.build_suggestions 对齐:
 *   【角色】+【角色设定摘要】+【最近对话 12 条】→ 3 条候选(JSON 数组)。
 * 失败时抛错,由调用方降级为"暂无推荐"(推荐只是输入辅助)。
 */
async function requestCustomSuggestions(
  settingsStore: ReturnType<typeof useSettingsStore>,
  characterName: string,
  history: Array<{ side: 'other' | 'mine'; text: string; image?: string }>,
): Promise<string[]> {
  const who = characterName || '角色'

  // 最近 12 条渲染成"我 / 角色:内容"(与后端同一口径;单条超 120 字截断)
  const lines: string[] = []
  for (const m of history.slice(-12)) {
    let content = (m.text ?? '').trim()
    if (!content) {
      if (!m.image) continue
      content = '[图片]'
    }
    const chars = Array.from(content)
    if (chars.length > 120) content = `${chars.slice(0, 120).join('')}…`
    lines.push(`${m.side === 'mine' ? '我' : who}：${content}`)
  }
  const convo = lines.join('\n') || '(暂无对话)'
  // 角色设定摘要:与单聊请求同一条角色提示词(沉浸式已在其中处理);
  // 长度占位符也要填掉 —— 递给模型的提示词里不该出现模板痕迹
  const persona = applyLengthPolicy(settingsStore.getCharacterMessage(who), who).slice(0, 2000)

  const user = `【角色】${who}\n`
    + `【角色设定摘要】\n${persona || '(无)'}\n\n`
    + `【最近对话】\n${convo}\n\n`
    + '请给出我接下来可以说的 3 句话。'

  const ask = async (extra = ''): Promise<string> => {
    const { text } = await customChatCompletion(settingsStore.apiConfig, [
      { role: 'system', content: SUGGEST_SYSTEM_SINGLE },
      { role: 'user', content: user + extra },
    ], {
      temperature: 0.9,
      maxTokens: 512,
      // 思考模式与单聊请求保持一致(此处无外部中止信号,超时自兜底)
      think: settingsStore.thinkEnabled,
      timeoutMs: 30_000,
    })
    return text
  }

  const out = parseSuggestionList(await ask(), 3, 25)
  // 模型偶发只给 1~2 条:补问一次(与后端 build_suggestions 一致)
  if (out.length < 3) {
    try {
      const extra = `\n\n注意:上一次只给了 ${out.length} 条,这次务必给出 3 条互不重复的。`
      for (const it of parseSuggestionList(await ask(extra), 3, 25)) {
        if (!out.includes(it)) out.push(it)
        if (out.length >= 3) break
      }
    } catch {
      // 补生成失败就用已有结果
    }
  }
  return out.slice(0, 3)
}

export function useAiChat() {
  const chatStore = useChatStore()
  const settingsStore = useSettingsStore()
  const usageStore = useUsageStore()
  // 压缩逻辑的唯一实现处(自动 / 手动共用),见 composables/useContextCompaction.ts
  const { compact } = useContextCompaction()
  // 回复逐拍显示(括号描写居中开启时,居中条与台词之间留间隔),单聊群聊共用
  const { appendStaged } = useReplyReveal()

  /**
   * 把"此刻这一段会话"抓成锁定目标
   *
   * 每个 AI 入口都必须在**第一个 await 之前**调用:构建请求要读人设/历史,
   * 而中间隔着网络(智能总结最长几十秒)。等 await 回来再读 activeSub,拿到的
   * 可能已经是另一段会话 —— A 的人设配 B 的历史,回复还会写进 B。
   */
  function lockTarget(): AiTurnTarget | null {
    const sub = chatStore.activeSub
    if (sub === null) return null
    const conv = chatStore.conversations[sub]
    if (!conv) return null
    return { sub, conv }
  }

  /**
   * 找回锁定会话**现在**的下标
   *
   * 结构变更(删一段 / 新建子对话 / 导入合并)会让会话在扁平数组里的位置整体
   * 位移。锁的是对象,所以能按对象找回它现在的位置 —— 回复仍写回同一段对话,
   * 而不是因为"下标变了"就把玩家刚发的消息丢掉。
   *
   * @returns 现在的下标;null = 这段会话已经被删除/替换,整轮应当放弃
   */
  function resolveTargetSub(target: AiTurnTarget): number | null {
    const list = chatStore.conversations
    if (list[target.sub] === target.conv) return target.sub
    const idx = list.indexOf(target.conv)
    return idx === -1 ? null : idx
  }

  /**
   * 触发 AI 流式回复(内部公共逻辑)
   *
   * 从当前对话读取角色信息 + 历史消息,构建请求。
   * - 后端模式(传入 backendInput):只传递原始数据(message/history/character),
   *   提示词与处理全部由后端 Python 脚本负责,一次性拿到 { reply } 后分段显示。
   * - 其他模式:沿用原有 system + 角色提示词 + OpenAI 格式流式调用。
   *
   * 流式缓冲完整回复后,按换行分段,逐段创建气泡顺序显示。
   * 每段之间有短暂假 loading 动画,模拟"逐条发送"的聊天节奏。
   *
   * @param backendInput   后端模式输入(非后端模式为 undefined)
   * @param userMsgId      用户消息 id(用于错误重试)
   * @param summaryHistory 经过智能总结的 history(非后端模式使用;为 undefined 时从 store 获取)
   * @param speakerOverride 指定说话人(群聊"让某角色发言一次"用;缺省 = 会话名,
   *                        单聊行为完全不变)
   * @param target         锁定目标会话(见 AiTurnTarget)。调用方若在 await 之前
   *                       抓过目标就必须传入;不传则退化为"此刻的当前会话"
   *                       (与改动前一致,兼容旧调用)
   */
  async function triggerAiResponse(
    backendInput?: BackendInput,
    userMsgId?: number,
    summaryHistory?: Array<{ side: 'other' | 'mine'; text: string; image?: string }>,
    speakerOverride?: SpeakerOverride,
    /**
     * 重新生成场景：新回复插到这条消息之后（原位置），而不是追加到末尾
     *
     * 由 regenerate() 传入「被重新生成那一轮的用户消息 id」。
     * 常规发送不传 → 走追加行为，与改动前完全一致。
     */
    insertAfterMessageId?: number,
    target?: AiTurnTarget,
  ): Promise<void> {
    // 目标会话:优先用调用方锁定的那段。锁定后才读人设/历史,并在写入前校验
    // 归属 —— "读取身份"和"写入位置"必须是同一段会话。
    const sub = target ? resolveTargetSub(target) : chatStore.activeSub
    if (sub === null) return
    const conv = target?.conv ?? chatStore.conversations[sub]
    if (!conv) return
    // 归属校验:锁定的会话已被删除 / 替换(不是"位移",位移会被 resolveTargetSub
    // 找回来)→ 整轮作废,绝不把回复写进同下标下的另一段会话
    if (chatStore.conversations[sub] !== conv) return
    // 群聊的会话名是群名(如「A、B和C的群聊」),不是角色名;
    // 有 speakerOverride 时必须用被点名的角色,否则会拿群名去查角色人设。
    const characterName = speakerOverride?.name ?? conv.name

    // 构建角色头像/名称:按**目标会话**取,不按"此刻选中的那段"取 ——
    // 等待回复期间玩家切走后,回复仍写回原会话,头像也该是原会话角色的
    const meta = chatStore.conversationMetaAt(sub)
    const speakerName = characterName
    const speakerAvatar = speakerOverride?.avatar ?? meta?.avatar ?? ''

    // 创建第一个 loading 气泡,并锁定本轮响应上下文:
    // 目标会话与说话人在此刻固定,之后切换会话 / 其他会话并发请求都不会串台。
    // 末位传 conv:store 会把它记为归属,后续每次写入都要求"还是这段会话"
    const ctx = chatStore.beginAiResponse(
      speakerName, speakerAvatar, sub, insertAfterMessageId, conv)
    if (!ctx) return

    /**
     * 落字前的"空回复"守卫
     *
     * 只拦**彻底空白**这一种:上游返回空、或被沉浸式过滤器整条吃掉。
     * 曾经还拦"只有动作/神态描写、没有台词"的回复(直接丢弃 + 报错),
     * 现已移除 —— 那属于角色的表达自由,不该由前端判死并吞掉内容。
     *
     * @returns true = 已按错误处理,调用方应立即 return
     */
    const rejectIfUnplayable = (): boolean => {
      if (fullText.trim()) return false
      chatStore.appendAiError(
        '回复内容为空(可能被沉浸式过滤掉或上游返回空),可点「重新生成」重试',
        userMsgId,
        ctx,
      )
      return true
    }

    // ---- 请求并缓冲完整回复(loading 动画持续,不实时显示文字) -------------
    let fullText = ''
    // 后端模式可选返回的心情表情 token(如 sns_emoji_001),缺失为 undefined
    let pendingMood: string | undefined
    // 服务商上报的用量(未上报时保持 null → 回退本地估算)
    let capturedUsage: NormalizedUsage | null = null
    // 后端上报的本轮**输出** token（记进会话用量；导出/统计会读它）
    // 说明：原来这个数还用于"后端模式该不该总结"的判定，那条链路已在
    // 2026-11-18 P0 删除，现在只作用量统计（不参与任何压缩决策）。
    let backendOutputTokens = 0
    // 后端上报的完整 usage（prompt_tokens 用于 lastPromptTokens / 累计 promptTokens）
    let backendUsage: BackendUsageLike | undefined
    // 本次请求是否成功拿到回复（用于 finally 里记账；提前 return 也要记）
    let requestSucceeded = false
    // 本地估算的输入 token(未乘校准比值,由 usage store 统一校准)
    let estimatedInputTokens = 0
    // 本轮是否发生了上下文压缩(压缩会使服务端缓存前缀失效)
    let compacted = false
    // 本轮回复是否已经通过 SSE **逐字实时**落进气泡(true 时跳过下面的分段播放)
    let streamedLive = false
    // 自定义 API 模式的历史估算 token（用于校准提示词占用）
    let customHistoryTokens = 0
    // usage 用 holder 持有：闭包内赋值，裸 let 会被 TS 控制流判成恒 null
    const usageHolder: { value: NormalizedUsage | null } = { value: null }

    try {
      try {
        if (backendInput) {
          // 后端模式 v4:统一网关 + character_id(不再发送长提示词)
          const characterId = CHARACTER_ID_MAP[characterName] || ''
          // 用户自定义提示词覆盖:仅存在覆盖时发送(后端优先使用覆盖)
          const hasOverride = !!settingsStore.promptOverrides[characterName]
          const request = buildBackendRequest(
            backendInput.message,
            characterName,
            backendInput.history,
            {
              characterId,
              // 这段对话的稳定 id：决定绑到哪一条服务端会话。
              // 少了它，同角色的两段对话会共用一个上下文（2026-11-19 修）。
              conversationKey: conversationKeyOf(conv),
              // 重新生成：让服务端**重写**这一轮，而不是把同一句再存一遍
              regenerate: backendInput.regenerate === true,
              // 用户覆盖才发送;内置提示词由后端按 character_id 加载
              characterPromptOverride: hasOverride
                ? settingsStore.getCharacterPrompt(characterName)
                : undefined,
              // 未收录角色(自定义):退回旧协议发送完整提示词
              systemPrompt: characterId
                ? undefined
                : (() => {
                    const fixed = settingsStore.getFullSystemPrompt()
                    const role = settingsStore.getCharacterPrompt(characterName)
                    // 通用规则里的长度占位符必须按角色填好再发(见 constants/prompts)
                    const rule = buildEmphasisRule(characterName)
                    const roleWithRule = role ? `${role}\n\n${rule}` : rule
                    return fixed ? `${fixed}\n\n【角色设定】\n${roleWithRule}` : roleWithRule
                  })(),
              think: settingsStore.thinkEnabled,
              // v7: 实验性功能-沉浸式对话模式(2026-10-03 语义修正:true=后端禁止括号动作,只留台词)
              immersiveMode: settingsStore.immersiveMode,
              // 实验性功能-使用新版提示词(后端按 characters_v2/ 加载)
              useNewPrompt: settingsStore.useNewPrompt,
              // 【2026-11-18 P0】不再发 force_search / use_rag / need_summary：
              // 新后端没有联网搜索，压缩也由服务端每轮自己做（见 docs/08）。
            },
          )
          // 本轮中止信号(SSE 与一次性请求共用同一个)
          const signal = chatStore.getAiSignal(ctx)

          if (settingsStore.sseStreaming) {
            // ---- 实验性:SSE 逐字流式(分条 + 分拍,"能播就先播") ----------------
            // 为什么不把 delta 原样直出:上游生成很快(实测一封 136 字的回复,95 个
            // 增量段全部在 1.03 秒内到达),直出在视觉上等于"一次性全出",反而丢掉
            // 了关闭 SSE 时逐拍显示的手感。
            // 做法:delta 先进缓冲;一旦缓冲里能切出**已经定稿**的段,就立刻用与
            // 关闭 SSE 时完全相同的那套机制播出去(appendStaged + 段间停顿 +
            // "正在输入"),剩下的段在 done 之后按权威全文补完。
            //
            // "定稿"的判据交给 splitAiSegmentsStreaming,不能再用 splitAiSegments:
            // 缓冲是半截的,未闭合括号里的句号会被当成句末断点,于是先播出一句
            // "（她笑了。"—— 这句话还会写进历史;等右括号到达后权威分段变少,
            // playedCount 多计,后半段就永远不显示了。
            let revealBuf = ''
            let streamDone = false
            let pacerStop = false
            let playedCount = 0

            /** 播出一段(节奏与关闭 SSE 时的分段播放完全一致) */
            const playSegment = async (text: string, isLast: boolean): Promise<void> => {
              if (!text) return
              if (playedCount === 0) {
                // 首段:填入当前的 loading 气泡(mood 此刻多半还没到,收尾时补写)
                chatStore.setPendingAiMood(pendingMood, ctx)
                await appendStaged(text, ctx)
              } else {
                // 后续段:先停顿,再亮"头像 + 加载气泡",再落字
                await delay(600 + Math.random() * 400)
                if (!chatStore.isCtxActive(ctx)) return
                chatStore.beginAiSegment(ctx)
                await delay(900 + Math.random() * 600)
                if (!chatStore.isCtxActive(ctx)) return
                await appendStaged(text, ctx)
              }
              playedCount++
              if (!chatStore.isCtxActive(ctx)) return
              if (isLast) chatStore.finishAiResponse(ctx)
              else chatStore.finishAiSegment(ctx)
            }

            const pacer = (async () => {
              while (!pacerStop) {
                if (!chatStore.isCtxActive(ctx)) return
                if (revealBuf.length === 0) {
                  if (streamDone) return
                  await delay(30)
                  continue
                }
                // 只播已定稿的段(未闭合括号内部、以及可能继续增长的末段都不播),
                // consumed 是"这几个段在缓冲里占了多少字符",据此切缓冲 ——
                // 不用 indexOf 猜位置(文本里有重复片段时会切错地方)。
                const { segments, consumed } = splitAiSegmentsStreaming(
                  revealBuf, settingsStore.smartSplit)
                if (consumed > 0) revealBuf = revealBuf.slice(consumed)
                if (segments.length > 0) {
                  for (const seg of segments) {
                    if (!chatStore.isCtxActive(ctx)) return
                    await playSegment(seg, false)
                  }
                  continue
                }
                if (streamDone) return
                await delay(30)
              }
            })()

            try {
              const result = await fetchBackendReplyStream(
                settingsStore.apiConfig,
                request,
                {
                  signal,
                  onDelta: (text) => {
                    revealBuf += text
                  },
                },
              )
              fullText = result.reply
              // 可选 mood 字段(token 形式,如 sns_emoji_001);缺失时为 undefined
              pendingMood = result.mood
              backendOutputTokens = pickOutputTokens(result.usage)
              backendUsage = result.usage
              requestSucceeded = true
              streamedLive = true
            } catch (err) {
              // 收到任何 delta 之前就失败(旧后端没有 /chat/stream、网络、非 2xx、
              // 响应不是 event-stream):回退到原来的非流式请求一次,用户无感。
              // 此时缓冲仍为空(揭示器一个字都没写过),回退后**照常走下面的分段
              // 播放**,与开关关闭时完全一致。
              if (!(err instanceof BackendStreamUnavailableError)) {
                pacerStop = true
                // 已收到的半截内容照常落进气泡(改造前 delta 是直出的,不能因为
                // 改成"节奏化播出"就把用户已经等到的字丢掉),再抛错交给错误气泡
                if (revealBuf) {
                  chatStore.appendAiChunk(revealBuf, ctx)
                  revealBuf = ''
                }
                throw err
              }
              const fallback = await fetchBackendReply(
                settingsStore.apiConfig,
                request,
                signal,
              )
              fullText = fallback.reply
              pendingMood = fallback.mood
              backendOutputTokens = pickOutputTokens(fallback.usage)
              backendUsage = fallback.usage
              requestSucceeded = true
            } finally {
              streamDone = true
            }
            await pacer

            // done 帧全文为空、但已经实时播过内容(后端异常):以已落下的字为准。
            // 各段在播放时已经各自 finishAiSegment 入了历史,这里只收尾 ——
            // 若交给下面的"空回复"守卫,玩家会看到"有内容 + 一条空回复错误"。
            if (streamedLive && !fullText.trim() && playedCount > 0) {
              if (chatStore.isCtxActive(ctx)) chatStore.finishAiResponse(ctx)
              return
            }

            if (streamedLive && rejectIfUnplayable()) return

            if (streamedLive) {
              // ---- 收尾:按权威全文补完剩余段 ----------------------------------
              // 已经播过 playedCount 段 —— 它们的文本逐字等于权威分段的前缀:
              // splitAiSegmentsStreaming 只消费"已经定稿"的分组(未闭合括号内部、
              // 以及可能被后续增量改写的末段都不消费)。剩下的段一律取自 done 帧
              // 的权威全文,首段气泡补上 mood。
              const finalSegs = splitAiSegments(fullText, settingsStore.smartSplit)
              const rest = finalSegs.slice(playedCount)
              chatStore.setFirstAiMood(pendingMood, ctx)
              for (let i = 0; i < rest.length; i++) {
                if (!chatStore.isCtxActive(ctx)) return
                await playSegment(rest[i], i === rest.length - 1)
              }
              if (chatStore.isCtxActive(ctx)) chatStore.finishAiResponse(ctx)
              return
            }
            // 回退路径(streamedLive 为 false):气泡里没有流式内容 → 落到下面的分段播放
          } else {
            // 开关关闭(默认):行为与改动前完全一致 —— 一次 JSON 请求拿完整回复
            const backendResult = await fetchBackendReply(
              settingsStore.apiConfig,
              request,
              signal,
            )
            fullText = backendResult.reply
            // 可选 mood 字段(token 形式,如 sns_emoji_001);缺失时为 undefined
            pendingMood = backendResult.mood
            backendOutputTokens = pickOutputTokens(backendResult.usage)
            backendUsage = backendResult.usage
            requestSucceeded = true
          }
        } else {
          // 原有模式:系统提示词 + 全局世界观 + 角色提示词 + 历史消息,SSE 流式调用
          // 两条 system 消息统一由 settings store 产出,保证与用量面板的统计口径一致
          const systemMessage = settingsStore.getSystemMessage()
          // 通用回复规则(EMPHASIS_RULE)由 settings 拼在角色提示词末尾,里面带着
          // 长度占位符 {LENGTH_TARGET}:必须在此按角色填好,否则模型收到的是一串
          // 模板痕迹。自定义 API 直连没有后端兜底,这里是最后一道。
          const characterMessage = applyLengthPolicy(
            settingsStore.getCharacterMessage(characterName), characterName)

          const isCustom = settingsStore.apiConfig.apiMode === 'custom'

          // 固定前缀指纹:用户改了世界观 / 角色提示词 → 缓存必然全部失效,
          // 面板据此给出提示,避免误判成"缓存没生效"。
          usageStore.notePrefix(hashText(`${systemMessage}\u0000${characterMessage}`))

          // custom 模式:预算检查 + 批量压缩,保证"未超预算时一条都不丢"
          // (默认模式不做任何前端压缩 —— 服务端每轮自己压，见 docs/08)
          // 压缩器按"当前会话"读写历史(useContextCompaction 只认 activeSub):
          // 玩家已经切走时不要压 —— 否则会把**另一段**对话的历史截短。
          // 跳过只意味着这次少回收一点预算,不丢内容。
          if (isCustom && chatStore.activeSub === sub) {
            compacted = (await compact(`${systemMessage}\n\n${characterMessage}`, 'auto')) !== null
          }

          // 使用经过智能总结的 history(如果提供),否则从 store 取实时历史。
          // custom 模式不传 summaryHistory → 取到的历史已含本轮输入(调用方先落消息),
          // 且顺序恒为 [system, system, ...历史] → 天然满足"前缀只追加"。
          // 必须按**锁定**的会话下标取:人设与历史要来自同一段对话
          const history = summaryHistory ?? chatStore.getChatHistory(sub)
          // 历史体积估算(未校准):自定义模式拿到真实 usage 后，用它反推提示词占用
          customHistoryTokens = estimateEntries(history)
          const messages = buildMessages(systemMessage, characterMessage, history)

          // 【2026-11-18 P0】自定义 API 的 RAG 检索整块删除：
          // 新后端 capabilities.web_search=false，检索服务（rag.peilika.beer）
          // 也不再是我们维护的入口；留着只会在每轮多打一次注定失败的请求。
          // 自定义模式的上下文只有「两条 system + 对话历史」，由用户自己的模型负责。

          // 本地估算(未校准),服务商未上报 usage 时由 usage store 乘以校准比值后展示
          estimatedInputTokens = estimateMessages(messages)

          // ---- 沉浸式对话的文本层过滤(自定义 API 直连没有后端兜底) --------------
          // 提示词层已在 getCharacterMessage 里处理(截掉强制括号的规则 + 追加禁止规则);
          // 这里再过一遍文本层:模型不听话时也不会漏出 (动作) / *动作*。
          // 用 ImmersiveFilter 逐增量过滤(跨 delta 保持括号状态),
          // 收尾时用一次性 stripImmersive 以权威全文兜底。
          const immersive = settingsStore.immersiveMode
          const immFilter = immersive ? new ImmersiveFilter(true) : null

          await streamChat({
            config: settingsStore.apiConfig,
            messages,
            signal: chatStore.getAiSignal(ctx),
            requestUsage: settingsStore.apiConfig.requestUsage,
            // 思考模式:仅自定义 API 模式透传(llm.ts 只认这一条直连链路)
            think: isCustom ? settingsStore.thinkEnabled : undefined,
            onUsage: (u) => {
              capturedUsage = u
              usageHolder.value = u
            },
            onChunk: (chunk) => {
              // 沉浸式:边收边滤(流式场景下真正的"跨增量保持状态")
              fullText += immFilter ? immFilter.feed(chunk) : chunk
            },
            onDone: (full) => {
              // 以权威全文为准再过滤一次(immersive=false 时原样返回)
              const filtered = stripImmersive(full, immersive)
              // 过滤成空时回退原文:沉浸式把整条回复都判定成"动作描写"时,
              // 玩家看到空气泡等于"对方没说话",比看到带括号的原文更糟。
              // 回退的原文若确实没有台词,会被下面的无台词守卫拦成可重试的错误。
              fullText = filtered || full
            },
          })
          requestSucceeded = true

          // ---- 实测提示词占用校准（自定义 API 模式，2026-11-14）-------------
          // 真实 usage.prompt_tokens − 本地估算的历史 token ≈ 提示词 + 框架开销。
          // 第一次拿到合理值就缓存到 localStorage（recordMeasuredPromptTokens 只写一次），
          // 之后触发判据优先用实测值；没有实测就退回写死的 BACKEND_PROMPT_TOKENS(4000)。
          // ⚠ 这里量的是**自定义 API 的 system 提示词**，与后端内置提示词长度不同：
          //    它只让"提示词占用"这个常量随真实端点收敛，不是后端提示词的精确值。
          if (isCustom && usageHolder.value && customHistoryTokens > 0) {
            const measured = usageHolder.value.inputTokens - customHistoryTokens
            if (recordMeasuredPromptTokens(measured)) {
              devWarn('[summary] 已缓存实测提示词占用 token:', Math.round(measured))
            }
          }
        }
      } catch (err) {
        // AbortError:用户主动中止,请求层返回空串而非抛错,此处仅兜底
        if (err instanceof DOMException && err.name === 'AbortError') {
          return
        }
        // 其他错误:创建错误占位气泡(红底 + "重新生成"按钮)。
        // 关键:错误消息只显示、绝不写入 contextHistory(不污染 AI 上下文,也不持久化)
        const errMsg = err instanceof Error ? err.message : String(err)
        chatStore.appendAiError(errMsg, userMsgId, ctx)
        return
      }

      // ---- 会话累计**输出** token（累加后端上报的 completion_tokens） ----
      // 只作用量统计与导出（原来还喂给"该不该总结"的判据，那条链已删）。
      if (backendInput && backendOutputTokens > 0) {
        chatStore.addSessionOutputTokens(backendOutputTokens)
      }

      // ---- 记录用量(仅非后端模式:后端模式的提示词在服务端,本地估算无意义) ----
      if (!backendInput) {
        usageStore.record({
          usage: capturedUsage,
          estimatedInput: estimatedInputTokens,
          estimatedOutput: estimateText(fullText),
          compacted,
          endpoint: settingsStore.apiConfig.baseUrl,
        })
      }

      // 本轮响应已失效(用户点停止,或同一会话又发起了新请求):不继续分段显示
      if (!chatStore.isCtxActive(ctx)) return

      // ---- "无台词 / 空回复"守卫:分段落字之前拦一次 --------------------------
      if (rejectIfUnplayable()) return

      // ---- 分段 --------------------------------------------------------------
      // 规则(换行 / 左括号前切断 + 纯括号段合并)的唯一实现处是 utils/aiText,
      // 单聊与群聊共用。此处曾内联一份等价拷贝,改规则要改两处、极易走散,已合并。
      // 是否在左括号前也切,由设置项「智能消息分条」决定(默认开启)。
      const segments = splitAiSegments(fullText, settingsStore.smartSplit)

      if (segments.length === 0) {
        // 无内容:结束响应(空的 loading 气泡会被 abortAiResponse 逻辑清理)
        chatStore.finishAiResponse(ctx)
        return
      }

      // ---- 第一段:填入当前 loading 气泡 -----------------------------------
      // 后端模式的心情表情(token)随首段消息一并写入(缺失时 appendAiChunk 拿到 undefined)
      chatStore.setPendingAiMood(pendingMood, ctx)
      // 分拍写入:开启「括号描写居中」时,居中条与台词之间留出间隔(见 useReplyReveal)
      await appendStaged(segments[0], ctx)
      if (!chatStore.isCtxActive(ctx)) return

      if (segments.length === 1) {
        // 单段:直接结束
        chatStore.finishAiResponse(ctx)
        return
      }

      // 多段:完成第一段(整体响应保持进行中)
      chatStore.finishAiSegment(ctx)

      // ---- 后续段:逐条顺序显示 ---------------------------------------------
      for (let i = 1; i < segments.length; i++) {
        // 本轮已失效检查(用户点停止,或同一会话又发起了新请求)
        if (!chatStore.isCtxActive(ctx)) break

        // 段间延迟(模拟"对方正在输入"的节奏)
        await delay(600 + Math.random() * 400)
        if (!chatStore.isCtxActive(ctx)) break

        // 复用同一上下文重新开启 loading 气泡
        // (不可改用 beginAiResponse:那会生成新上下文并让本轮被判为陈旧)
        chatStore.beginAiSegment(ctx)

        // 假 loading 动画展示(模拟"对方正在输入"的节奏)
        await delay(900 + Math.random() * 600)
        if (!chatStore.isCtxActive(ctx)) break

        // 填入本段文字(同样按部件分拍)
        await appendStaged(segments[i], ctx)
        if (!chatStore.isCtxActive(ctx)) break

        // 最后一段:结束整体响应;中间段:保持响应状态
        if (i < segments.length - 1) {
          chatStore.finishAiSegment(ctx)
        } else {
          chatStore.finishAiResponse(ctx)
        }
      }

      // 如果循环中途 break(用户中止),确保状态清理
      if (chatStore.isCtxActive(ctx)) {
        chatStore.finishAiResponse(ctx)
      }

    } finally {
      // ---- 真实 prompt 用量记账（2026-11-14）-------------------------------
      // 必须放 finally：下面有多处提前 return（流式提前播完 / 空回复），
      // 只有放这里才能保证"请求成功后每次都累计"，不会漏记。
      // 判定只用上一轮 usage.prompt_tokens；没有 usage 的旧会话才退回
      // "写死提示词 + 字符估算"。
      if (requestSucceeded && chatStore.conversations[sub] === conv) {
        let promptUsed = 0
        if (backendInput) {
          // 后端 usage.prompt_tokens 已经包含提示词 + 完整历史
          promptUsed = pickRealPromptTokens(backendUsage)
          if (promptUsed <= 0) {
            promptUsed = BACKEND_PROMPT_TOKENS
              + estimateEntries(backendInput.history)
              + estimateText(backendInput.message)
          }
        } else {
          // capturedUsage 在 onUsage 回调里赋值，裸变量会被 TS 收窄成 null，
          // 这里读 holder（与 requestCustomSummary 同一处理）。
          const normalized = usageHolder.value
          promptUsed = pickRealPromptTokens(normalized ? {
            prompt_tokens: normalized.inputTokens,
            total_tokens: normalized.inputTokens + normalized.outputTokens,
          } : null)
          // 没有 usage 时用本地的字符估算（estimateMessages 含两条 system）
          if (promptUsed <= 0) promptUsed = estimatedInputTokens
        }
        if (promptUsed > 0) chatStore.recordPromptUsage(promptUsed, sub)
      }

      // 【2026-11-18 P0】这里原来有一段「总结收尾」：
      //   收到服务端 summary → applyBackendSummary(...)
      //   没收到（新后端永远收不到）→ planRoundTrim 本地把历史整轮裁到 60%
      //                              + noteSummaryFailure 弹"总结失败，已跳过"
      // 新后端每轮自己压（只压不裁），前端这两条都是净损失，已整段删除。
      // 自定义 API 模式的总结收尾在 maybeCustomSummary() 内部完成，不在这里。
    }
  }

  /**
   * 发送文本消息并触发 AI 流式回复
   *
   * 完整流程:
   *   1. 检查 API 配置(未配置时抛出错误,由调用方引导用户配置)
   *   2. 获取历史并应用智能总结(超 50 条时)
   *   3. 后端模式:构建 backendInput
   *   4. 其他模式:传递 summaryHistory 给 triggerAiResponse
   *   5. 添加用户消息并触发 AI 响应
   *
   * @param text 用户输入文本
   * @throws API 未配置时抛出错误
   */
  async function sendAndWaitForAi(text: string): Promise<void> {
    // 锁定目标会话(第一个 await 之前):下面的智能总结可能要等几十秒,
    // 期间玩家切走的话,人设/历史/写入位置都必须仍然是"发起时那一段"
    const target = lockTarget()
    if (!target) return

    // 检查 API 配置
    if (!settingsStore.isApiConfigured) {
      throw new Error('API 未配置：请先在设置中填写 Base URL、API Key 和模型名')
    }

    // 获取历史并按模式决定是否裁剪(custom 模式返回 undefined,见 planHistory)
    const rawHistory = chatStore.getChatHistory(target.sub)
    const trimmedHistory = await planHistory(rawHistory, settingsStore, chatStore, target.sub)

    // 根据模式构建请求参数
    const isBackend = settingsStore.apiConfig.apiMode === 'backend'
    let backendInput: BackendInput | undefined
    let summaryHistory: Array<{ side: 'other' | 'mine'; text: string; image?: string }> | undefined

    if (isBackend) {
      // 默认模式:构建 backendInput(历史必须实打实给出,后端不读本地 store)
      // 【2026-11-18 P0】不再带 needSummary / contextWindowTokens：
      // 压缩由服务端每轮自己做，前端只负责把最近的历史递过去。
      backendInput = {
        message: text,
        // 必须复制一份：contextHistory 是同一个数组引用，下面 sendUserMessage 会
        // 往它里面 push 这句 —— 不复制的话，请求体里的"历史"会带上正要发的那一句，
        // 服务端于是把它当历史导进去、再当新消息写一遍（"你怎么叫了我两次"）。
        history: (trimmedHistory ?? rawHistory).slice(),
      }
    } else {
      // 非后端模式:传递裁剪结果(custom 模式为 undefined → 由内部压缩后现取)
      summaryHistory = trimmedHistory
    }

    // 等待期间那段会话可能已位移(删了别的对话)或被删掉:按对象找回它现在的位置。
    // 找不回来 = 这段对话已经没了,什么都不写(总好过写进别人的对话)
    const sub = resolveTargetSub(target)
    if (sub === null) return

    // 1. 添加用户消息(含上下文历史同步),记录其 id 供错误重试使用
    const userMsg = chatStore.sendUserMessage(text, sub)

    // 2. 触发 AI 响应(目标仍锁定同一个会话对象,由 triggerAiResponse 再解析一次)
    await triggerAiResponse(backendInput, userMsg?.id ?? undefined, summaryHistory,
      undefined, undefined, { sub, conv: target.conv })
  }

  /**
   * 群聊:让「指定发言」里选中的角色单独说一次
   *
   * 与 sendAndWaitForAi 的区别:这里**不追加用户消息** —— 只是在群里催
   * 被点名的角色发一条,所以直接调 triggerAiResponse 并用 speakerOverride
   * 把说话人换成该角色。
   *
   * 说明:群聊的后端协议尚未最终确定,这里复用的是单聊那套请求构造
   * (角色人设 / 历史 / 实验性开关),后端模式下 message 传空串表示
   * "没有新话题,继续"。若后端需要专门的群聊协议,这里是唯一的对接点。
   */
  async function speakAsCharacter(name: string, avatar: string): Promise<void> {
    // 锁定目标会话(见 lockTarget):远程总结调用之后,人设与历史仍须同源
    const target = lockTarget()
    if (!target) return

    if (!settingsStore.isApiConfigured) {
      throw new Error('API 未配置：请先在设置中填写 Base URL、API Key 和模型名')
    }

    const rawHistory = chatStore.getChatHistory(target.sub)
    const trimmedHistory = await planHistory(rawHistory, settingsStore, chatStore, target.sub)
    const isBackend = settingsStore.apiConfig.apiMode === 'backend'

    // 群聊「指定发言」本轮不接入后端总结（契约只覆盖单聊 /chat）；
    // 历史仍由 buildBackendRequest 截最近 30 条，不会无限增长。
    const backendInput: BackendInput | undefined = isBackend
      ? { message: '', history: trimmedHistory ?? rawHistory }
      : undefined
    const summaryHistory = isBackend ? undefined : trimmedHistory

    await triggerAiResponse(backendInput, undefined, summaryHistory, { name, avatar },
      undefined, target)
  }

  /**
   * 图片发送后触发 AI 流式回复
   *
   * 图片消息已由 store.sendImage 添加(含上下文历史同步),
   * 此方法仅负责触发 AI 响应流程。
   *
   * @throws API 未配置时抛出错误
   */
  async function respondAfterImage(): Promise<void> {
    // 锁定目标会话:非后端模式下面还要等智能总结,期间切走会拿错历史
    const target = lockTarget()
    if (!target) return

    // 检查 API 配置
    if (!settingsStore.isApiConfigured) {
      throw new Error('API 未配置：请先在设置中填写 Base URL、API Key 和模型名')
    }

    // 获取历史(图片消息已写入 contextHistory)
    const rawHistory = chatStore.getChatHistory(target.sub)

    // 根据模式构建请求参数
    const isBackend = settingsStore.apiConfig.apiMode === 'backend'
    let backendInput: BackendInput | undefined
    let summaryHistory: Array<{ side: 'other' | 'mine'; text: string; image?: string }> | undefined

    if (isBackend) {
      // 默认模式:图片消息保留在 history 中(让后端能识别图片)。
      // 历史由 buildBackendRequest 截最近 30 条；压缩由服务端做。
      backendInput = {
        message: '[图片]',
        history: rawHistory,
      }
    } else {
      // 非后端模式:按模式裁剪(custom 模式返回 undefined → 由内部压缩后现取)
      summaryHistory = await planHistory(rawHistory, settingsStore, chatStore, target.sub)
    }

    // 图片消息已由 sendImage 添加,直接触发 AI 响应(定位刚发送的图片消息 id)
    const lastMine = [...target.conv.messages].reverse().find((m) => m.side === 'mine')
    await triggerAiResponse(backendInput, lastMine?.id, summaryHistory, undefined, undefined, target)
  }

  /**
   * 重新生成(错误气泡按钮 / 长按菜单"重新生成"共用)
   *
   * 以指定的用户消息为"本轮输入",重新触发 AI:
   * - 后端模式:message = 用户消息文本(图片消息为 "[图片]"),
   *   history = 当前 contextHistory 移除该用户消息条目(错误不写历史、AI 回复已由
   *   prepareRegenerate 清理,故此时 history 末尾即该用户消息)
   * - 其他模式:应用智能总结后传递给 triggerAiResponse
   *
   * @param userMsgId 本轮用户消息 id
   */
  async function regenerate(userMsgId: number): Promise<void> {
    // 锁定目标会话:重新生成同样要等总结 API,期间切走会让"重生成某一条"落到
    // 另一段会话(而且锚点消息 id 在那边根本不存在)
    const target = lockTarget()
    if (!target) return
    const userMsg = target.conv.messages.find((m) => m.id === userMsgId)
    if (!userMsg) return

    const isBackend = settingsStore.apiConfig.apiMode === 'backend'
    const isImage = !!userMsg.image

    if (isBackend) {
      // 后端模式:message 与 history 分离
      let history = chatStore.getChatHistory(target.sub)
      if (!isImage) {
        // 文字消息:移除末尾的用户消息条目(与正常发送流程一致:message 与 history 分离)
        if (history.length > 0 && history[history.length - 1].side === 'mine') {
          history = history.slice(0, history.length - 1)
        }
      }
      // 图片消息:保留图片条目在 history 中(与 respondAfterImage 行为一致,让后端能识别图片)。
      // 【2026-11-18 P0】不再判定/请求前端总结 —— 服务端每轮自己压。
      await triggerAiResponse(
        {
          message: isImage ? '[图片]' : userMsg.text,
          history,
          // 关键：告诉服务端这是"重写这一轮的回话"，不是"再说一遍同一句话"
          regenerate: true,
        },
        userMsgId,
        undefined,
        undefined,
        // 关键：新回复插回这条用户消息之后（prepareRegenerate 已删掉旧回复），
        // 否则会 append 到对话末尾 —— 重新生成第 1 条却跑到第 3 条下面。
        userMsgId,
        target,
      )
    } else {
      // 非后端模式:用户消息已在 contextHistory 中,按模式裁剪后重新触发
      // (custom 模式返回 undefined → 由内部压缩后现取,否则压缩结果会被盖掉)
      const rawHistory = chatStore.getChatHistory(target.sub)
      const summaryHistory = await planHistory(rawHistory, settingsStore, chatStore, target.sub)
      await triggerAiResponse(undefined, userMsgId, summaryHistory, undefined, userMsgId, target)
    }
  }

  /**
   * 按需拉取单聊「AI 推荐回复」(与群聊 useGroupChat.fetchSuggestions 对齐)
   *
   * **一段对话只生成一次**:推荐随对话一起存进 cards(见
   * chatStore.setConversationSuggestions),只要最后一条消息没变,反复点开面板
   * 都直接复用缓存,不再打 API;重开软件也从对话数据里读回来。
   * 玩家又发了消息(对话状态变了)才会重新生成一次。
   *
   * 拉取失败 / 开关关闭 / 无会话都返回 false —— 推荐只是输入辅助,绝不因此报错,
   * 面板显示"暂无推荐选项"的空状态即可。
   *
   * @param force 忽略缓存强制重新生成(留给"换一批"这类显式操作)
   * @returns 是否拿到了候选
   */
  async function fetchSuggestions(force = false): Promise<boolean> {
    // 「AI 推荐回复」实验性开关关闭时按钮本就不渲染,这里再兜一层
    if (!settingsStore.choicesEnabled) return false
    const sub = chatStore.activeSub
    if (sub === null) return false
    const conv = chatStore.conversations[sub]
    if (!conv) return false

    // 已有缓存且对话没往前走 → 直接用,不重复生成
    const tail = chatStore.tailMessageId()
    if (!force) {
      const cached = chatStore.getConversationSuggestions()
      if (cached && cached.items.length > 0 && cached.forMessageId === tail) {
        chatStore.setPendingChoices(cached.items.map((label) => ({ label })))
        return true
      }
    }

    const characterName = conv.name
    // 与单聊 /chat 请求同一口径的历史(只取有上下文的聊天记录;函数内部再截 50 条)
    const history = chatStore.getChatHistory()
    // custom 模式必须完全绕开后端网关(审计修复,见 requestCustomSuggestions)
    const isCustom = settingsStore.apiConfig.apiMode === 'custom'
    try {
      const items = isCustom
        ? await requestCustomSuggestions(settingsStore, characterName, history)
        : await fetchBackendSuggestions({
            characterId: CHARACTER_ID_MAP[characterName] || '',
            character: characterName,
            // 推荐也要落到**这段**对话上，否则会去猜同角色的另一条会话
            conversationKey: conversationKeyOf(conv),
            history,
            useNewPrompt: settingsStore.useNewPrompt,
            immersiveMode: settingsStore.immersiveMode,
          })
      // 等待期间开关可能被关掉 / 会话可能被切换:以此刻的状态为准。
      // 必须比一次 sub:store 的 activeSub watch 只在"切换那一刻"换过一次列表,
      // 迟到的结果若直接写入,会把别的会话的推荐显示到当前会话的面板里。
      if (!settingsStore.choicesEnabled) return false
      if (chatStore.activeSub !== sub) return false
      // 写入这段对话的持久化数据(空结果不写,下次打开还能重试)
      chatStore.setConversationSuggestions(items, tail)
      return items.length > 0
    } catch (e) {
      // 推荐失败只记日志,不弹错误、不影响聊天
      devWarn('[chat] 拉取推荐失败:', e)
      return false
    }
  }

  /** 中止当前 AI 响应(中止流式请求 + 停止后续分段显示) */
  function abort() {
    chatStore.abortAiResponse()
  }

  return {
    sendAndWaitForAi,
    speakAsCharacter,
    respondAfterImage,
    regenerate,
    fetchSuggestions,
    abort,
  }
}
