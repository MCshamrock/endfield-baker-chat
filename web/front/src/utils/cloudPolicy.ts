// =============================================================================
// cloudPolicy —— 「登录后要不要自动上传本机存档」的判定（纯函数，零 import）
// -----------------------------------------------------------------------------
// 单独一个文件的理由：`scripts/list-e2e.mjs` 要用 Node 直接跑它。
//   · cloudSync.ts 会链到 nativeStorage（Capacitor）和 apiClient，
//     那些在 Node 里打包不起来（也不需要 —— 这是策略，不是 IO）；
//   · 这条判定错一次的代价是"在云端建重复会话"，必须能被自动化断言钉住。
//
// 产品口径（2026-11-20 三叶草）：数据优先存/读本机；登录是"有需要才做"的动作，
// 登录之后自动把本机存档传上服务器。
// =============================================================================

/** 判定输入 */
export interface UploadDecisionInput {
  /** 账号层状态 */
  accountState: 'unknown' | 'guest' | 'user'
  /** 本机有没有东西可传（检测旧数据 = 读本机卡库） */
  hasLocalData: boolean
  /** 本机消息条数（0 = 只有空卡，不值得传） */
  localMessages: number
  /** marker 里记的账号 id（没有则 null） */
  uploadedUserId: string | null
  /** 当前账号 id（访客为空串） */
  currentUserId: string
}

/** 判定结果 */
export interface UploadDecision {
  run: boolean
  /** 为什么不传（run=false 时给人看/打日志用） */
  reason: string
}

/**
 * 登录这一刻该不该自动上传本机存档
 *
 * 规则（顺序即优先级）：
 *   · 不是已登录 → 不传（访客不催人注册）
 *   · 本机没东西 / 只有空卡 → 不传（传个空包没意义）
 *   · 这个账号已经传过 → 不传（`POST /v1/imports` 只按包 sha256 去重，
 *     本机继续聊天后包内容就变了，再传会在云端**再建一遍会话**）
 *   · 其余 → 传
 */
export function planUploadOnLogin(input: UploadDecisionInput): UploadDecision {
  if (input.accountState !== 'user') {
    return { run: false, reason: '还没登录，先不传（登录后会自动上传）' }
  }
  if (!input.hasLocalData || input.localMessages <= 0) {
    return { run: false, reason: '本机没有聊天记录，不用上传' }
  }
  if (input.uploadedUserId && input.uploadedUserId === input.currentUserId) {
    return { run: false, reason: '这个账号已经上传过本机存档了' }
  }
  return { run: true, reason: '' }
}
