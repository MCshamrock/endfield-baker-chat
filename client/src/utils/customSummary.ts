// =============================================================================
// 自定义 API 的前端总结（customSummary，2026-11-14 新增）
// -----------------------------------------------------------------------------
// 适用场景：apiMode === 'custom' 且「智能总结」开启、且上一轮真实 usage 占用
// 达到上下文窗口的 80%。此时前端用用户自己配置的 OpenAI 兼容端点直连生成总结。
// （默认模式不走这里：服务端每轮自己压，前端不再请求任何总结。）
//
// 长度公式（与后端 summary_service.compute_budget 同一口径）：
//   token 预算  = 上下文窗口 × 10%
//   字数上限    = clamp(token 预算 × 1.6 字/token, 200, 1200)
//   max_tokens  = clamp(ceil(字数上限 / 1.6 × 1.35) + 96, 200, 1600)
//
// 上下文窗口取值（优先 → 兜底）：
//   1. apiConfig.contextWindow（用户手填，>0 才用）
//   2. models.dev 模型库里的该模型窗口（utils/modelsDev.lookupModelContext）
//   3. 32768（取不到时的保守值，避免总结请求本身把窗口撑爆）
//
// 提示词与结构解析与后端 summary_service 保持一致（见 constants/summaryPrompt.ts
// 的同步警告）：50% 重要记忆节点 / 40% 情节脉络 / 10% 角色当前状态。
// =============================================================================

import type { ApiConfig } from '../stores/settings'
import type { NormalizedUsage } from './usage'
import { customChatCompletion } from './customApi'
import { lookupModelContext } from './modelsDev'
import {
  CUSTOM_SUMMARY_RETRY_REMINDER,
  CUSTOM_SUMMARY_SYSTEM_PROMPT,
} from '../constants/summaryPrompt'

/** 上下文窗口取不到时的兜底值（token） */
export const CUSTOM_SUMMARY_FALLBACK_CONTEXT = 32768

/** 总结长度预算：上下文总长的 10% */
export const CUSTOM_SUMMARY_RATIO = 0.10

/** 总结字数下限 / 上限 */
export const CUSTOM_SUMMARY_MIN_CHARS = 200
export const CUSTOM_SUMMARY_MAX_CHARS = 1200

/** 中文换算：1 token ≈ 1.6 字（与 utils/tokenEstimate 同一口径） */
export const CJK_CHARS_PER_TOKEN = 1.6

/** 总结请求超时（ms）：20s 内拿不到就按"已跳过"降级，不阻断本轮 */
export const CUSTOM_SUMMARY_TIMEOUT_MS = 20_000

/** 总结输入最多占窗口的 70%（与后端 SUMMARY_INPUT_WINDOW_RATIO 一致） */
const SUMMARY_INPUT_WINDOW_RATIO = 0.70

/** max_tokens 的格式余量（标题 / 换行） */
const SUMMARY_MAX_TOKENS_OVERHEAD = 96

/** 模型库查询最多等多久：等太久会拖慢聊天，超时就退回 32768 */
const LIBRARY_LOOKUP_TIMEOUT_MS = 2_500

/** 一次总结的上下文来源（设置页会标注） */
export type CustomSummaryContextSource = 'config' | 'library' | 'fallback'

/** 上下文解析结果 */
export interface CustomSummaryContext {
  contextWindow: number
  source: CustomSummaryContextSource
}

/** 已命中的模型库窗口缓存（key = baseUrl|model），避免每轮重复查询 */
const libraryHitCache = new Map<string, number>()

/** 给一个 Promise 加超时；超时返回 null（原始请求继续跑，下次可能命中缓存） */
function withTimeout<T>(task: Promise<T>, ms: number): Promise<T | null> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => resolve(null), ms)
    task.then(
      (value) => {
        window.clearTimeout(timer)
        resolve(value)
      },
      () => {
        window.clearTimeout(timer)
        resolve(null)
      },
    )
  })
}

/**
 * 解析本次总结要用的上下文窗口
 *
 * @param config 自定义 API 配置（apiMode === 'custom'）
 */
export async function resolveCustomSummaryContext(config: ApiConfig): Promise<CustomSummaryContext> {
  // 1) 用户手填的窗口优先
  if (Number.isFinite(config.contextWindow) && config.contextWindow > 0) {
    return { contextWindow: Math.floor(config.contextWindow), source: 'config' }
  }

  const model = (config.model ?? '').trim()
  if (!model) {
    return { contextWindow: CUSTOM_SUMMARY_FALLBACK_CONTEXT, source: 'fallback' }
  }

  // 2) 模型库（models.dev）
  const key = `${config.baseUrl ?? ''}\u0000${model}`
  const cached = libraryHitCache.get(key)
  if (cached && cached > 0) {
    return { contextWindow: cached, source: 'library' }
  }
  const hit = await withTimeout(
    lookupModelContext(model, config.baseUrl ?? ''),
    LIBRARY_LOOKUP_TIMEOUT_MS,
  )
  if (hit && Number.isFinite(hit.contextWindow) && hit.contextWindow > 0) {
    libraryHitCache.set(key, hit.contextWindow)
    return { contextWindow: hit.contextWindow, source: 'library' }
  }

  // 3) 取不到 → 保守值
  return { contextWindow: CUSTOM_SUMMARY_FALLBACK_CONTEXT, source: 'fallback' }
}

/**
 * 由上下文窗口算总结字数上限
 *
 * 例：131072 → 上下文×10% = 13107 token → ×1.6 = 20971 字 → 夹到 1200 字；
 *     2000    → 200 token → 320 字 → 夹到 [200, 1200] 内 = 320 字。
 */
export function summaryCharLimit(contextWindow: number): number {
  const window = Number.isFinite(contextWindow) && contextWindow > 0
    ? contextWindow
    : CUSTOM_SUMMARY_FALLBACK_CONTEXT
  const tokenBudget = Math.max(1, Math.floor(window * CUSTOM_SUMMARY_RATIO))
  const charBudget = Math.floor(tokenBudget * CJK_CHARS_PER_TOKEN)
  return Math.max(CUSTOM_SUMMARY_MIN_CHARS, Math.min(CUSTOM_SUMMARY_MAX_CHARS, charBudget))
}

/** 由字数上限反推 max_tokens（给标题 / 换行留余量，避免状态段被截断） */
export function summaryMaxTokens(charLimit: number): number {
  const value = Math.ceil(charLimit / CJK_CHARS_PER_TOKEN * 1.35) + SUMMARY_MAX_TOKENS_OVERHEAD
  return Math.max(200, Math.min(1600, value))
}

/** 设置页展示行：自定义 API 上下文 ≈ X token · 总结上限 ≈ Y 字 */
export function formatCustomSummaryInfoLine(contextWindow: number): string {
  const window = Number.isFinite(contextWindow) && contextWindow > 0
    ? Math.round(contextWindow)
    : CUSTOM_SUMMARY_FALLBACK_CONTEXT
  return `自定义 API 上下文 ≈ ${window} token · 总结上限 ≈ ${summaryCharLimit(window)} 字`
}

/** 把历史渲染成"用户：… / 角色：…"的纯文本（图片用 [图片] 占位） */
export function renderHistoryForSummary(
  history: Array<{ side: 'other' | 'mine'; text: string; image?: string }>,
): string {
  const lines: string[] = []
  for (const msg of history) {
    const content = (msg.text ?? '').trim() || (msg.image ? '[图片]' : '')
    if (!content) continue
    lines.push(`${msg.side === 'mine' ? '用户' : '角色'}：${content}`)
  }
  return lines.join('\n')
}

/**
 * 总结输入超长时只保留最近的部分（从旧到新截头）
 *
 * 与后端 _truncate_input 同一口径：最多占窗口的 70%，避免总结请求本身撑爆。
 */
export function truncateSummaryInput(text: string, contextWindow: number): string {
  const window = Number.isFinite(contextWindow) && contextWindow > 0
    ? contextWindow
    : CUSTOM_SUMMARY_FALLBACK_CONTEXT
  const maxChars = Math.floor(window * SUMMARY_INPUT_WINDOW_RATIO * CJK_CHARS_PER_TOKEN)
  if (maxChars <= 0 || text.length <= maxChars) return text
  const omitted = text.length - maxChars
  let kept = text.slice(-maxChars)
  // 从下一个换行处开始，避免把一句话切成两半
  const nl = kept.indexOf('\n')
  if (nl >= 0 && nl < 200) kept = kept.slice(nl + 1)
  return `（更早的约 ${omitted} 字对话已省略，以下为最近的部分）\n${kept}`
}

/** 把 {char_limit} 等占位符填成具体字数（与后端同一换算） */
export function buildSummarySystemPrompt(charLimit: number): string {
  const stateChars = Math.max(60, Math.floor(charLimit * 0.10))
  const memoryChars = Math.floor(charLimit * 0.50)
  const plotChars = Math.max(80, Math.floor(charLimit * 0.40))
  return CUSTOM_SUMMARY_SYSTEM_PROMPT
    .split('{memory_chars}').join(String(memoryChars))
    .split('{plot_chars}').join(String(plotChars))
    .split('{state_chars}').join(String(stateChars))
    .split('{char_limit}').join(String(charLimit))
}

// =============================================================================
// 结构化输出解析（与后端 summary_service.parse_sections / enforce_length 一致）
// =============================================================================

/** 三段标题（顺序固定） */
const SECTION_TITLES: Array<[keyof SummarySections, string]> = [
  ['memory', '重要记忆节点'],
  ['plot', '情节脉络'],
  ['state', '角色当前状态'],
]

/** 总结三段 */
export interface SummarySections {
  memory: string
  plot: string
  state: string
}

const HEADER_RE = /^[\s#>*\-·]*(?:(?:\d+[.、)]|[一二三四五六]、))?\s*[【\[]?\s*(重要记忆节点|情节脉络|角色当前状态)\s*[】\]]?\s*[:：]?\s*(.*)$/

/** 把模型输出解析成三段；容忍标题变体与标题同行正文 */
export function parseSummarySections(text: string): SummarySections {
  const sections: SummarySections = { memory: '', plot: '', state: '' }
  let current: keyof SummarySections | null = null
  const titleToKey: Record<string, keyof SummarySections> = {
    重要记忆节点: 'memory',
    情节脉络: 'plot',
    角色当前状态: 'state',
  }
  for (const rawLine of (text ?? '').split('\n')) {
    const line = rawLine.replace(/\s+$/, '')
    const m = HEADER_RE.exec(line)
    if (m) {
      current = titleToKey[m[1]]
      const rest = (m[2] ?? '').trim()
      if (rest && current) sections[current] += `${rest}\n`
      continue
    }
    if (current) sections[current] += `${line}\n`
  }
  return {
    memory: sections.memory.trim(),
    plot: sections.plot.trim(),
    state: sections.state.trim(),
  }
}

/** 按固定顺序拼回带标题的三段文本 */
export function assembleSummarySections(sections: SummarySections): string {
  const parts: string[] = []
  for (const [key, title] of SECTION_TITLES) {
    const body = (sections[key] ?? '').trim()
    if (body) parts.push(`【${title}】\n${body}`)
  }
  return parts.join('\n\n').trim()
}

/**
 * 把总结裁到字数上限内
 *
 * 裁剪顺序刻意是"先情节脉络、再重要记忆节点"：【角色当前状态】是解决串台
 * 的关键，永远完整保留。与后端 enforce_length 同规则。
 */
export function enforceSummaryLength(text: string, charLimit: number): string {
  if (text.length <= charLimit) return text
  const sections = parseSummarySections(text)
  const state = sections.state.trim()
  const memory = sections.memory.trim()
  const plot = sections.plot.trim()
  if (!state && !memory && !plot) {
    return `${text.slice(0, Math.max(0, charLimit - 1)).replace(/\s+$/, '')}…`
  }

  const titleCost = '【重要记忆节点】'.length + '【情节脉络】'.length + '【角色当前状态】'.length + 6
  const remain = Math.max(0, charLimit - titleCost - state.length)
  const memoryBudget = Math.floor(remain * 0.55)
  const plotBudget = Math.max(0, remain - memoryBudget)

  const cut = (value: string, budget: number): string => {
    if (value.length <= budget) return value
    let sliced = value.slice(0, Math.max(0, budget - 1))
    const nl = sliced.lastIndexOf('\n')
    if (nl > 0) sliced = sliced.slice(0, nl)
    return `${sliced.replace(/\s+$/, '')}…`
  }

  return assembleSummarySections({
    memory: cut(memory, memoryBudget),
    plot: cut(plot, plotBudget),
    state,
  })
}

// =============================================================================
// 请求
// =============================================================================

/** 一次自定义总结的结果 */
export interface CustomSummaryResult {
  /** 已按结构整理 + 截到字数上限的总结正文 */
  text: string
  /** 本次（可能两次尝试）累计的 usage.prompt_tokens；没上报为 0 */
  promptTokens: number
  /** 最后一次上报的归一化 usage（排查用） */
  usage: NormalizedUsage | null
  /** 本次使用的上下文窗口 */
  contextWindow: number
  /** 本次的总结字数上限 */
  charLimit: number
}

/** 读一次调用的 prompt token（没有则 total；再没有 0） */
function usagePromptTokens(usage: NormalizedUsage | null): number {
  if (!usage) return 0
  if (Number.isFinite(usage.inputTokens) && usage.inputTokens > 0) return usage.inputTokens
  const total = usage.inputTokens + usage.outputTokens
  return Number.isFinite(total) && total > 0 ? total : 0
}

/**
 * 前端直连自定义 API 生成总结
 *
 * @throws 配置缺失 / 20s 超时 / HTTP 非 2xx / 返回为空 → 调用方按"已跳过"降级
 */
export async function requestCustomSummary(
  config: ApiConfig,
  history: Array<{ side: 'other' | 'mine'; text: string; image?: string }>,
  contextWindow: number,
): Promise<CustomSummaryResult> {
  const charLimit = summaryCharLimit(contextWindow)
  const systemPrompt = buildSummarySystemPrompt(charLimit)
  const rendered = truncateSummaryInput(renderHistoryForSummary(history), contextWindow)
  if (!rendered.trim()) throw new Error('没有可总结的对话内容')

  const userPrompt = `以下是需要整理的对话（从旧到新，共 ${history.length} 条）：\n\n${rendered}`
  const maxTokens = summaryMaxTokens(charLimit)
  const deadline = Date.now() + CUSTOM_SUMMARY_TIMEOUT_MS
  // usage 用 holder 持有：闭包内赋值，裸 let 会被 TS 控制流判成恒零/恒 null
  const promptTokensHolder: { value: number } = { value: 0 }
  const usageHolder: { value: NormalizedUsage | null } = { value: null }

  const call = async (extra: string): Promise<string> => {
    const remaining = deadline - Date.now()
    if (remaining <= 500) throw new Error('总结请求超时')
    const result = await customChatCompletion(config, [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userPrompt + extra },
    ], {
      temperature: 0.25,
      maxTokens,
      // 总结不需要思考模式（慢且贵），与后端 _call_model 一致
      think: false,
      timeoutMs: remaining,
    })
    promptTokensHolder.value += usagePromptTokens(result.usage ?? null)
    usageHolder.value = result.usage ?? null
    return result.text
  }

  let raw = await call('')
  let sections = parseSummarySections(raw)
  let score = SECTION_TITLES.filter(([key]) => sections[key].trim()).length

  // 三段缺一 → 用一次"格式纠正"重试（时间还够才重试，不拖过 20s 总预算）
  if (score < SECTION_TITLES.length && deadline - Date.now() > 3_000) {
    try {
      const second = await call(CUSTOM_SUMMARY_RETRY_REMINDER)
      const secondSections = parseSummarySections(second)
      const secondScore = SECTION_TITLES.filter(([key]) => secondSections[key].trim()).length
      if (secondScore >= score) {
        raw = second
        sections = secondSections
        score = secondScore
      }
    } catch {
      // 第二次失败就用第一次的结果（结构可能不全，但仍可读）
    }
  }

  const assembled = assembleSummarySections(sections) || raw.trim()
  const text = enforceSummaryLength(assembled, charLimit)
  if (!text.trim()) throw new Error('总结返回内容为空')
  return {
    text,
    promptTokens: promptTokensHolder.value,
    usage: usageHolder.value,
    contextWindow,
    charLimit,
  }
}
