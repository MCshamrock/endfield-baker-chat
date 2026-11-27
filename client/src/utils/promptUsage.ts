// =============================================================================
// 真实 prompt 用量的小工具（promptUsage，2026-11-14 新增）
// -----------------------------------------------------------------------------
// 纯函数层：把"取哪个字段 / 怎么累加 / 用哪个值判定"收敛到一处，
// 方便单测，也避免 store、useAiChat、设置页三处口径走散。
// =============================================================================

/** 服务商 / 后端 usage 的最小形状（只关心 prompt 相关字段） */
export interface PromptUsageLike {
  /** 输入（提示词 + 历史）token 总数 */
  prompt_tokens?: number
  /** 总 token（prompt + completion）；没有 prompt_tokens 时用它兜底 */
  total_tokens?: number
  /** 部分 OpenAI 兼容端点用 input_tokens 表示输入 */
  input_tokens?: number
}

/** 读一个非负有限数，否则返回 0 */
function readToken(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : 0
}

/**
 * 从原始 usage 里取"本轮真实 prompt 占用"
 *
 * 顺序：prompt_tokens → input_tokens → total_tokens → 0（调用方再用字符估算兜底）。
 * 说明：total_tokens 含 completion，但在"没有 prompt_tokens"时它仍比本地估算更接近
 * 真实占用，符合需求里"没有则 total_tokens"的约定。
 */
export function pickRealPromptTokens(usage?: PromptUsageLike | null): number {
  if (!usage || typeof usage !== 'object') return 0
  return readToken(usage.prompt_tokens)
    || readToken(usage.input_tokens)
    || readToken(usage.total_tokens)
}

/**
 * 累加会话的 prompt_tokens
 *
 * 非法 / 负数 / 0 一律不改变原值；缺省从 0 起算。
 */
export function accumulatePromptTokens(
  previous: number | undefined,
  delta: number,
): number {
  const base = typeof previous === 'number' && Number.isFinite(previous) && previous > 0
    ? previous
    : 0
  const add = Number.isFinite(delta) && delta > 0 ? delta : 0
  return Math.round(base + add)
}

/**
 * 判定时取哪个值
 *
 * 有真实值（上一轮 usage）就用真实值 —— 它已经包含提示词 + 历史；
 * 没有真实值（旧会话 / 首次请求）才退回"写死提示词 + 字符估算"。
 *
 * @param lastPromptTokens 会话记录的上一轮真实 prompt 占用（0 = 没有）
 * @param fallbackEstimate 无真实值时的本地估算（写死提示词 + 历史估算）
 */
export function resolvePromptOccupancy(
  lastPromptTokens: number | undefined,
  fallbackEstimate: number,
): number {
  const real = typeof lastPromptTokens === 'number' && Number.isFinite(lastPromptTokens)
    && lastPromptTokens > 0
    ? lastPromptTokens
    : 0
  if (real > 0) return Math.round(real)
  const est = Number.isFinite(fallbackEstimate) && fallbackEstimate > 0
    ? fallbackEstimate
    : 0
  return Math.round(est)
}

/**
 * 是否达到总结阈值
 *
 * @param occupancy 当前占用（真实 prompt 或估算值）
 * @param contextWindow 上下文窗口
 * @param triggerRatio 触发占比（缺省 0.8）
 */
export function reachesSummaryThreshold(
  occupancy: number,
  contextWindow: number,
  triggerRatio = 0.8,
): boolean {
  if (!Number.isFinite(occupancy) || occupancy <= 0) return false
  if (!Number.isFinite(contextWindow) || contextWindow <= 0) return false
  if (!Number.isFinite(triggerRatio) || triggerRatio <= 0) return false
  return occupancy >= contextWindow * triggerRatio
}
