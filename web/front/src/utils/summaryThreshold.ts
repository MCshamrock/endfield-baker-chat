// =============================================================================
// 总结触发阈值的精细度边界（summaryThreshold，2026-11-14 追加）
// -----------------------------------------------------------------------------
// 纯函数层：只做百分比算术，不碰 store / DOM，便于单独验证。
//
// 设计（与需求逐条对应）：
//   硬下限 45%：再低就总结过频，白烧上游额度；
//   硬上限 80%：再高就没有空间留给本轮回复；
//   动态上限 = clamp(45, 100 − 提示词占比 − 10, 80)
//     · 10% 是给本轮回复预留的冗余；
//     · 提示词占比越大，能设的触发阈值越低（否则"提示词 + 本轮回复"会撑爆窗口）。
//
// ⚠ 需求原文把 `100 − 提示词占比 − 10` 写成"min 的算法"，但从两处可以确认
//   它实际约束的是**上限**：
//     1. 示例 `上下文 131072 · 提示词 ≈4000（3.1%）· 可调范围 45%–80%`
//        —— 3.1% 代入该式得 86.9%，若当 min 会被压到 79%，与示例的 45% 矛盾；
//     2. "10% 冗余"只会限制阈值不能太高，不会限制阈值不能太低。
//   因此这里实现为：45% 固定为下限，动态值作为上限（最高仍不超过 80%）。
// =============================================================================

import {
  SUMMARY_REPLY_RESERVE_PERCENT,
  SUMMARY_TRIGGER_MAX_PERCENT,
  SUMMARY_TRIGGER_MIN_PERCENT,
} from '../constants/summaryBudget'

/** 一次精细度边界计算的结果 */
export interface SummaryTriggerBounds {
  /** 可调下限（固定 45%） */
  min: number
  /** 可调上限（≤80%，随提示词占比动态压低） */
  max: number
  /** 提示词 token */
  promptTokens: number
  /** 上下文窗口 token */
  contextWindow: number
  /** 提示词占上下文的百分比（0~100） */
  promptPercent: number
  /** 未夹取的"安全上限"：100 − 提示词占比 − 10 */
  safeBound: number
}

/** 读一个有限的非负数，否则返回 0 */
function nonNegative(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0
}

/** 把任意值夹到 [min, max] 并取整 */
export function clampPercent(value: number, min = SUMMARY_TRIGGER_MIN_PERCENT,
  max = SUMMARY_TRIGGER_MAX_PERCENT): number {
  const lo = Math.min(min, max)
  const hi = Math.max(min, max)
  const n = Number.isFinite(value) ? Math.round(value) : SUMMARY_TRIGGER_MAX_PERCENT
  return Math.min(hi, Math.max(lo, n))
}

/**
 * 计算当前模式 / 当前提示词占用下的可调范围
 *
 * @param promptTokens  固定提示词（+ 用户自定义提示词）的 token 占用
 * @param contextWindow 当前模式的上下文窗口
 */
export function computeSummaryTriggerBounds(
  promptTokens: number,
  contextWindow: number,
): SummaryTriggerBounds {
  const prompt = nonNegative(promptTokens)
  const window = nonNegative(contextWindow)
  const promptPercent = window > 0 ? (prompt / window) * 100 : 0
  const safeBound = 100 - promptPercent - SUMMARY_REPLY_RESERVE_PERCENT
  // 上限：安全值夹在 [45, 80] 之间；窗口未知时按 80% 处理
  const max = window > 0
    ? clampPercent(safeBound, SUMMARY_TRIGGER_MIN_PERCENT, SUMMARY_TRIGGER_MAX_PERCENT)
    : SUMMARY_TRIGGER_MAX_PERCENT
  return {
    min: SUMMARY_TRIGGER_MIN_PERCENT,
    max,
    promptTokens: prompt,
    contextWindow: window,
    promptPercent,
    safeBound,
  }
}

/** 把持久化的阈值夹到当前可调范围（旧值 / 提示词变化后都走这里） */
export function clampSummaryTriggerPercent(
  value: number | undefined,
  bounds: SummaryTriggerBounds,
): number {
  const raw = typeof value === 'number' && Number.isFinite(value)
    ? value
    : SUMMARY_TRIGGER_MAX_PERCENT
  return clampPercent(raw, bounds.min, bounds.max)
}

/** 触发阈值折算成 token：上下文 × 百分比 */
export function triggerTokenEstimate(contextWindow: number, percent: number): number {
  const window = nonNegative(contextWindow)
  const pct = Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : 0
  return Math.round(window * pct / 100)
}

/** 百分比 → 0..1 比例（触发判定用） */
export function summaryTriggerRatio(percent: number | undefined): number {
  const pct = typeof percent === 'number' && Number.isFinite(percent) ? percent : 80
  return clampPercent(pct) / 100
}
