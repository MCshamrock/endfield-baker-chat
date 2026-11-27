// =============================================================================
// 智能总结的写死预算（summaryBudget）
// -----------------------------------------------------------------------------
// 2026-11-14 按新架构重做：这两项**不允许用户配置**，写死在前端常量里。
//
// 为什么写死：
//   旧实现让用户在「智能总结 → 上下文总长」里填 512000，又和 apiConfig.contextWindow
//   (默认 0 → 65536) 打架，触发判据成了 3000 + 历史 ≥ 512000×0.8，实际几乎永不触发。
//   现在只有一套口径：提示词占用 + 当前历史 token ≥ 窗口 × 80% 就总结。
//
// 上下文窗口为什么取 131072：
//   Agnes 2.5-flash 的真实窗口官方未公布（旧代码按 512K 写，但没有实测依据）。
//   131072（128K）是"宁可早一点总结，也不要把请求撑爆"的保守值。
//   ⚠ 该值待用真实 usage 校准：等拿到稳定的 prompt_tokens 上报后，按
//   实际可用窗口替换这里的数字（同步更新 summary_service.DEFAULT_CONTEXT_WINDOW_TOKENS）。
// =============================================================================

/**
 * 后端提示词占用的 token 数（写死）
 *
 * 组成：后端内置世界观 + 角色设定 + 回复风格规则 + 前端两条 system 提示词。
 * 这是"不参与总结、每轮都原样重发"的那部分，必须在触发判据里预先扣掉。
 *
 * ⚠ 后端提示词实测占用，改这里要同步后端（summary_service / build_dialog_messages）。
 * 初值 4000；自定义 API 模式拿到真实 usage 后，会把实测值缓存在 localStorage，
 * 之后优先用实测值（见下方 effectiveBackendPromptTokens）。
 */
export const BACKEND_PROMPT_TOKENS = 4000

/**
 * 后端上下文总长（token，写死）
 *
 * Agnes 2.5-flash 的窗口未知 → 先取 131072 作为保守值。
 * ⚠ 该值待用真实 usage 校准；改这里要同步后端 summary_service 的缺省窗口。
 */
export const BACKEND_CONTEXT_WINDOW_TOKENS = 131072

/** 触发总结的占比：提示词 + 历史达到窗口的 80% 就总结（旧默认值） */
export const SUMMARY_TRIGGER_RATIO = 0.8

/**
 * 「精细度调整」可调区间的两个端点（2026-11-14 追加）
 *
 * - 下限 45%：再低就会过于频繁地总结，白烧上游额度；
 * - 上限 80%：再高就没有空间给本轮回复，容易刚总结完又立刻触顶。
 *
 * 实际可调上限还会被"100% − 提示词占比 − 10% 回复冗余"进一步压低
 * （见 utils/summaryThreshold.computeSummaryTriggerBounds）。
 */
export const SUMMARY_TRIGGER_MIN_PERCENT = 45
export const SUMMARY_TRIGGER_MAX_PERCENT = 80

/** 给本轮回复预留的窗口占比（百分比）：计算可调上限时扣掉 */
export const SUMMARY_REPLY_RESERVE_PERCENT = 10


/**
 * 总结后保留的最近消息条数（N = 15）
 *
 * 与 buildBackendRequest 的默认尾部窗口（BACKEND_HISTORY_LIMIT = 30）无关：
 * 这里指"上下文历史条目"条数，15 条 ≈ 7~8 轮问答，足够让总结后的对话无缝接上，
 * 又给总结本身留出空间。数值偏大：模型能直接看到更多近处细节；
 * 偏小：总结承担更多记忆。15 是实测下来回复连贯性与 token 体积的折中。
 */
export const SUMMARY_KEEP_RECENT = 15

/** 等待总结的兜底时长（ms）：超过就提示"总结超时，已跳过"并恢复输入框 */
export const SUMMARY_WAIT_TIMEOUT_MS = 30000

/**
 * 实测提示词 token 的 localStorage key
 *
 * 自定义 API 模式每轮都能拿到真实 usage.prompt_tokens；
 * 减去本地估算的历史体积，就是"提示词 + 框架开销"的实测值。
 * 第一次拿到合理值就缓存下来（后续不再覆盖），没有就退回写死的 4000。
 */
const MEASURED_PROMPT_TOKENS_KEY = 'endfield-baker-prompt-tokens-measured'

/** 实测值允许区间：太小说明测量失败，太大说明把历史算进了提示词 */
const MEASURED_MIN = 512
const MEASURED_MAX = 65536

/** 读取实测的提示词 token（没有/非法时返回 null） */
export function measuredPromptTokens(): number | null {
  try {
    const raw = localStorage.getItem(MEASURED_PROMPT_TOKENS_KEY)
    if (!raw) return null
    const n = Number(raw)
    if (!Number.isFinite(n) || n < MEASURED_MIN || n > MEASURED_MAX) return null
    return Math.round(n)
  } catch {
    return null
  }
}

/**
 * 当前生效的提示词 token（优先实测值，没有则用写死值）
 *
 * 设置面板展示、触发判据都用它，保证两处口径一致。
 */
export function effectiveBackendPromptTokens(): number {
  return measuredPromptTokens() ?? BACKEND_PROMPT_TOKENS
}

/**
 * 记录一次实测的提示词 token
 *
 * 只在**第一次**拿到合理值时写入（"第一次成功后缓存，后续优先用实测值"）：
 * 反复覆盖的话，某次上游把缓存命中算进 prompt_tokens 就会把常量带偏。
 *
 * @param measured 实测值（prompt_tokens − 本地估算的历史 token）
 * @returns 是否写入
 */
export function recordMeasuredPromptTokens(measured: number): boolean {
  if (!Number.isFinite(measured)) return false
  const value = Math.round(measured)
  if (value < MEASURED_MIN || value > MEASURED_MAX) return false
  if (measuredPromptTokens() !== null) return false
  try {
    localStorage.setItem(MEASURED_PROMPT_TOKENS_KEY, String(value))
    return true
  } catch {
    return false
  }
}
