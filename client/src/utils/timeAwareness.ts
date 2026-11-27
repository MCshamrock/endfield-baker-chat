// =============================================================================
// 时间感知（timeAwareness）—— 「角色知道现在几点吗」这一个开关
// -----------------------------------------------------------------------------
// 语义（不是"让它别报时"，是"它根本不知道"）：
//   · 开（默认）：每轮把玩家设备上的本地时间发给后端，提示词里会出现系统时钟；
//   · 关：请求里 `time_enabled:false` 且 `local_time` 发空串。后端会**丢掉时间**
//     并额外告诉模型「你没有时间信息，被问到就说不知道，不许编」。
//
// 为什么存在 localStorage 而不是 settings store：
//   它要在 utils/backend.ts / groupBackend.ts 这两个「不依赖 Pinia 的请求层」里读，
//   直接读 localStorage 比给请求层塞一个 store 依赖干净得多（与 memoryApi 的
//   新建会话偏好同一个思路）。
//
// 默认值 = **开**：老版本前端一直在发时间，升级后行为不该变；读失败也按开处理。
// =============================================================================

/** localStorage key（跟着这台设备走，不跟着账号走） */
export const TIME_AWARE_KEY = 'baker_time_aware'

/**
 * 读「时间感知」开关
 *
 * 没存过 / localStorage 被禁用（隐私模式）/ 值不认识 → 一律 true。
 * 宁可让角色知道时间，也不要因为读不到配置就突然"变成不知道"。
 */
export function readTimeAware(): boolean {
  try {
    const raw = localStorage.getItem(TIME_AWARE_KEY)
    if (raw === '0' || raw === 'off' || raw === 'false') return false
    return true
  } catch {
    return true
  }
}

/** 写「时间感知」开关（写失败静默：本次会话仍按当前内存值工作） */
export function writeTimeAware(on: boolean): void {
  try {
    localStorage.setItem(TIME_AWARE_KEY, on ? '1' : '0')
  } catch {
    // 静默失败
  }
}
