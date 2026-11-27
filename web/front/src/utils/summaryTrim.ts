// =============================================================================
// 按"整轮"裁剪历史（summaryTrim，2026-11-14 新增）
// -----------------------------------------------------------------------------
// 纯函数层：只做分组 + 算术，不碰 store / DOM，便于单独推演与单测。
//
// 为什么不能按"条"裁：
//   模型看到的消息必须成对（一条 mine 提问 + 其后连续的 other 回复）。
//   若从头部按条裁，很容易留下一条孤零零的 other —— 模型会把它当成
//   "角色先开口"，一问一答被打断，行为直接跑偏。
//
// 规则（与需求逐条对应）：
//   1. 一轮 = 一条 mine + 其后连续的 other（群聊式灌水也算同一轮）；
//   2. 从最旧的一轮开始整轮丢弃，直到「预计占用 ≤ 目标」（默认 60% 上下文）；
//   3. 至少保留最近 6 轮（不足 6 轮则全留，绝不因为预算把它们丢掉）；
//   4. 历史头部若以 other 开头（上一次裁剪留下的半轮），把它连同后面连续的
//      other 一起丢掉，保证保留段的第一条永远是 mine；
//   5. 幂等：对同一份历史连续调用两次，结果一致；不改变条目顺序 / 内容。
// =============================================================================

import { estimateEntries, estimateEntry } from './tokenEstimate'
import { SUMMARY_ENTRY_PREFIX } from './contextBudget'

/** 可裁剪的历史条目（与 stores/chat 的 contextHistory 元素同形） */
export interface TrimEntry {
  side: 'other' | 'mine'
  text: string
  image?: string
}

/** 裁剪目标：丢到预计占用 ≤ 上下文 × 60%（留 40% 给本轮回复与下一轮输入） */
export const TRIM_TARGET_RATIO = 0.6

/** 至少保留的最近轮数（不足则全留） */
export const TRIM_MIN_KEEP_ROUNDS = 6

/** 裁剪参数 */
export interface RoundTrimOptions {
  /** 上下文总长（token） */
  contextWindow: number
  /** 目标占比；缺省 60% */
  targetRatio?: number
  /** 至少保留的最近轮数；缺省 6 */
  minKeepRounds?: number
  /** 固定前缀（两条 system 提示词）的 token 占用，计入"预计占用" */
  fixedTokens?: number
  /** 估算自校准比值（与 usageStore.calibration 同口径，缺省 1） */
  ratio?: number
}

/** 一轮 */
export interface HistoryRound {
  /** 该轮的第一条（恒为 mine） */
  mine: TrimEntry
  /** 其后的连续 other */
  others: TrimEntry[]
  /** 该轮全部条目（mine 在前） */
  entries: TrimEntry[]
}

/** 裁剪结果 */
export interface RoundTrimResult {
  /** 裁剪后应进入请求的历史（头部可能是【对话总结】条目） */
  kept: TrimEntry[]
  /** 实际丢弃的条目数（含头部孤儿 other） */
  dropped: number
  /** 丢弃的轮数 */
  droppedRounds: number
  /** 保留的轮数（不含【对话总结】头部） */
  keptRounds: number
  /** 头部被丢掉的孤儿 other 条数 */
  leadingOrphans: number
  /** 裁剪后预计占用（固定前缀 + 头部总结 + 保留历史） */
  estimatedTokens: number
  /** 本次裁剪的目标占用 */
  targetTokens: number
}

/** 这条历史是不是压缩产生的【对话总结】冻结摘要 */
export function isSummaryEntry(entry: TrimEntry | undefined): boolean {
  return !!entry && entry.side === 'mine' && typeof entry.text === 'string'
    && entry.text.startsWith(SUMMARY_ENTRY_PREFIX)
}

/**
 * 把历史切成"头部总结 + 孤儿 other + 整轮"
 *
 * @param entries 历史条目（最旧在前）
 */
export function splitHistoryRounds(entries: TrimEntry[]): {
  /** 头部的【对话总结】条目（存在时永远保留，不参与轮次裁剪） */
  summaryHead: TrimEntry | null
  /** 第一条 mine 之前孤立的 other（无法成轮，必须丢） */
  leadingOrphans: TrimEntry[]
  /** 问答轮（每轮第一条恒为 mine） */
  rounds: HistoryRound[]
} {
  const summaryHead = isSummaryEntry(entries[0]) ? entries[0] : null
  const body = summaryHead ? entries.slice(1) : entries
  const leadingOrphans: TrimEntry[] = []
  const rounds: HistoryRound[] = []

  for (const entry of body) {
    if (entry.side === 'mine') {
      rounds.push({ mine: entry, others: [], entries: [entry] })
    } else if (rounds.length > 0) {
      const round = rounds[rounds.length - 1]
      round.others.push(entry)
      round.entries.push(entry)
    } else {
      // 第一条 mine 之前的 other：问答被切断的残段
      leadingOrphans.push(entry)
    }
  }

  return { summaryHead, leadingOrphans, rounds }
}

/** 历史里有多少"整轮"（不含头部总结、不计孤儿 other） */
export function countHistoryRounds(entries: TrimEntry[]): number {
  return splitHistoryRounds(entries).rounds.length
}

/**
 * 规划一次整轮裁剪
 *
 * 算法（伪代码）：
 *   groups = split(entries)                 // [总结头] + 孤儿 + 轮[]
 *   kept = 全部轮；target = context × 0.6
 *   while 保留轮数 > 6 且 预计占用 > target:
 *       丢掉最旧的一轮
 *   kept = [总结头] + 保留轮
 *
 * "至少 6 轮"是硬下限：即使 6 轮本身仍超 60%，也不再往下丢。
 * 这时候只能靠模型窗口硬扛，或由用户调大窗口 —— 总比把最近剧情拆散好。
 */
export function planRoundTrim(entries: TrimEntry[], opts: RoundTrimOptions): RoundTrimResult {
  const contextWindow = Number.isFinite(opts.contextWindow) && opts.contextWindow > 0
    ? opts.contextWindow
    : 0
  const ratio = Number.isFinite(opts.ratio) && (opts.ratio ?? 0) > 0 ? (opts.ratio as number) : 1
  const targetRatio = Number.isFinite(opts.targetRatio) && (opts.targetRatio ?? 0) > 0
    ? (opts.targetRatio as number)
    : TRIM_TARGET_RATIO
  const requestedMin = Number.isFinite(opts.minKeepRounds) && (opts.minKeepRounds ?? 0) >= 0
    ? Math.floor(opts.minKeepRounds as number)
    : TRIM_MIN_KEEP_ROUNDS
  const fixedTokens = Number.isFinite(opts.fixedTokens) && (opts.fixedTokens ?? 0) > 0
    ? Math.ceil(opts.fixedTokens as number)
    : 0

  const { summaryHead, leadingOrphans, rounds } = splitHistoryRounds(entries)
  const headTokens = summaryHead ? Math.ceil(estimateEntry(summaryHead) * ratio) : 0

  // 先算每轮体积（一次算好，避免循环里反复估算）
  const roundTokens = rounds.map((round) =>
    round.entries.reduce((sum, e) => sum + Math.ceil(estimateEntry(e) * ratio), 0),
  )
  let bodyTokens = roundTokens.reduce((sum, t) => sum + t, 0)
  const targetTokens = Math.max(0, Math.round(contextWindow * targetRatio))
  const minKeep = Math.min(requestedMin, rounds.length)

  let start = 0
  let estimatedTokens = fixedTokens + headTokens + bodyTokens
  while (rounds.length - start > minKeep && estimatedTokens > targetTokens) {
    bodyTokens -= roundTokens[start]
    start++
    estimatedTokens = fixedTokens + headTokens + bodyTokens
  }

  const keptRounds = rounds.slice(start)
  const keptBody = keptRounds.flatMap((round) => round.entries)
  const kept = summaryHead ? [summaryHead, ...keptBody] : keptBody

  return {
    kept,
    dropped: entries.length - kept.length,
    droppedRounds: start,
    keptRounds: keptRounds.length,
    leadingOrphans: leadingOrphans.length,
    estimatedTokens: fixedTokens + headTokens + bodyTokens,
    targetTokens,
  }
}

/** 计算某组条目的估算 token（供调用方展示/断言，不乘额外系数） */
export function estimateTrimEntries(entries: TrimEntry[]): number {
  return estimateEntries(entries)
}
