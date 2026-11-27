// =============================================================================
// 列表数据源的"门槛判定"（listSource，纯函数）
// -----------------------------------------------------------------------------
// 2026-11-20 产品口径改成**本地优先、账号可选**：
//
//   · 任何设备首次进入都读**本机存档**（不登录也能聊、也有记录）；
//   · "云端"不再是另一种"列表视图"，而是一次**同步动作**：
//       切到云端 = 把服务端里本机没有的会话补进来（合并，不覆盖），
//       点「上传」/ 登录 = 把本机存档传上云。
//   · 因为没有"整棵替换本地卡树"这回事了（合并只加不改，见
//     utils/conversationSync.mergeServerCards），原先那道"有旧数据没迁移就不许切
//     server"的门禁也失去了意义 —— 它挡住了最需要云端的人，还让他们以为开关坏了。
//
// 保留这些函数（而不是删掉）有两个原因：
//   1. stores/list.ts 与设置页还在用同一批判定，改签名会牵连很多地方；
//   2. `scripts/list-e2e.mjs` 用它们做"策略回归"，口径变了就改断言，
//      但"判定与写入分离"（先判定、后写 localStorage）这条不能丢。
// =============================================================================

export type ListSource = 'local' | 'server'

/** 判定所需的两条事实 */
export interface ListGateState {
  /**
   * 这台设备有没有旧本地数据
   *
   * null = 还没检测过（刚启动）。检测是异步的（读 IndexedDB），
   * 所以 null 时按"有旧数据"处理：宁可让用户手动切，也不能先隐藏他的本地卡。
   */
  hasLegacyData: boolean | null
  /** 迁移标记 `bakerai.legacy-migrated.v1` 是否为真 */
  migrated: boolean
}

/**
 * 允许不允许切云端
 *
 * 【2026-11-20】恒为 true。旧判据（"有旧数据且没迁移 → 拒绝"）是给
 * "整棵替换本地卡树"那版设计的；现在合并只加不改，切过去不会丢任何东西。
 */
export function canUseServerSource(_state: ListGateState): boolean {
  return true
}

/**
 * 首次运行时的默认数据源
 *
 * 【2026-11-20】恒为 local。以前"新设备默认 server"是为了让人一登录就看到云端
 * 数据，代价是本地零副本；现在改成默认本机 + 登录/切云端时**合并**，
 * 新设备照样能看到云端记录（见 listStore.ensureLoaded 的云端补齐），
 * 而且本机从此一直有一份。
 */
export function pickInitialSource(_hasLegacyData: boolean | null): ListSource {
  return 'local'
}

/** setSource 的判定结果 */
export interface SetSourcePlan {
  /** 允许切吗 */
  ok: boolean
  /** 允许时才写 localStorage —— **先判定、后写入**（被拒时一个字节都不改） */
  write: boolean
  /** 被拒的原因（直接显示给用户） */
  reason: string
}

/**
 * 切换判定（纯函数，同时决定"要不要写存储"）
 *
 * 2026-11-18 修（verify-front 复验 FAIL 2）：旧实现先 `localStorage.setItem`
 * 再 `return false`，于是"被拒"之后开关显示 server、实际走 local，自相矛盾。
 * 现在把"判定"与"写入"分开：`write === false` 时调用方不许碰 localStorage。
 *
 * 2026-11-20：不再有"被拒"的分支（见 canUseServerSource），但接口保留 ——
 * 调用方仍然按 plan.ok / plan.write 走，将来要加门禁只改这里。
 */
export function planSetSource(next: ListSource, _state: ListGateState): SetSourcePlan {
  if (next !== 'server' && next !== 'local') {
    return { ok: false, write: false, reason: '未知的数据源' }
  }
  return { ok: true, write: true, reason: '' }
}

/**
 * 登录 / 注册成功后要不要**自动**切到云端
 *
 * 【2026-11-20】恒为 false：登录不再改变"列表从哪读"（永远读本机），
 * 登录只做两件事 —— 上传本机存档、把云端独有的会话补进本机。
 * 保留函数是为了让调用方语义清晰（"登录不自动切视图"是一条明确的决定，
 * 不是一个可以顺手删掉的空函数）。
 */
export function shouldAutoSwitchOnLogin(
  _state: { userSet: boolean; requestedSource: ListSource },
  _gate: ListGateState,
): boolean {
  return false
}
