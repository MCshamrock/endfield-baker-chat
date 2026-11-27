// =============================================================================
// cloudSync —— 「登录后自动上传本机存档」那一层（2026-11-20 本地优先）
// -----------------------------------------------------------------------------
// 产品口径（三叶草 2026-11-20）：
//   数据优先存/读在本机；登录是"有需要才做"的动作，登录之后**自动**把本机存档
//   传上服务器（不用再自己找"立即迁移"按钮）。
//
// 三个约束，决定了这里的写法：
//
//   1. **只传一次**。上传走的是 `POST /v1/imports`，服务端只按"包内容 sha256"
//      去重（见 web/back app/imports/service.py）。本机一旦继续聊天，包内容就变了，
//      再传一次会在云端**再建一遍会话**（重复的"佩丽卡 1 / 佩丽卡 2"）。
//      所以用 marker 记住"这个账号已经传过"，同一个账号不重复上传。
//      之后的聊天走的是服务端接口，云端本来就跟着更新，不需要再整包传。
//
//   2. **认账号不认设备**。换一个人登录（或换号）时 marker 不匹配 → 允许再传一次。
//
//   3. **没登录就不传**（访客不催人注册）。判定写成纯函数 `planUploadOnLogin()`，
//      `scripts/list-e2e.mjs` 直接测它，不需要浏览器。
// =============================================================================

import { devInfo, devWarn } from './logger'
import { hasDeviceToken } from './apiClient'
import { detectLegacyLocal, migrateLegacyLocal } from './legacyLocalData'
import { planUploadOnLogin } from './cloudPolicy'

export { planUploadOnLogin } from './cloudPolicy'
export type { UploadDecision, UploadDecisionInput } from './cloudPolicy'

/** marker 的存储键（localStorage） */
export const CLOUD_UPLOAD_KEY = 'baker_cloud_upload_v1'

/** 上传成功后记下来的事实 */
export interface CloudUploadMarker {
  /** 上传时间（ms） */
  at: number
  /** 当时是哪个账号（没登录时是空串） */
  userId: string
  /** 传上去的会话 / 消息条数（给界面显示"上次传了 N 条"） */
  conversations: number
  messages: number
}

// ---- marker 读写（隐私模式一律静默失败）------------------------------------

export function readUploadMarker(): CloudUploadMarker | null {
  try {
    const raw = localStorage.getItem(CLOUD_UPLOAD_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CloudUploadMarker
    if (!parsed || typeof parsed.at !== 'number') return null
    return parsed
  } catch {
    return null
  }
}

function writeUploadMarker(marker: CloudUploadMarker): void {
  try {
    localStorage.setItem(CLOUD_UPLOAD_KEY, JSON.stringify(marker))
  } catch {
    // 隐私模式 / 配额满：下次登录会再传一遍（服务端按包 sha256 去重，
    // 只要中间没继续聊天，重复上传不会建重复会话）
  }
}

/** 清掉 marker（"我要重传一遍"时用） */
export function clearUploadMarker(): void {
  try {
    localStorage.removeItem(CLOUD_UPLOAD_KEY)
  } catch {
    // 忽略
  }
}

/**
 * 上传本机存档到当前账号
 *
 * 实际动作就是老的 `migrateLegacyLocal()`（它读的就是本机 `endfield-baker`
 * 卡库，打包成导入包 POST 到 `/v1/imports`）。这里只多做三件事：
 * 判定、计数、写 marker。
 *
 * @returns `{ uploaded, reason, conversations, messages }`
 */
export async function uploadLocalNow(options: {
  userId: string
  /** 忽略"已经传过"（设置页的「重新上传」用） */
  force?: boolean
  onProgress?: (text: string) => void
}): Promise<{
  uploaded: boolean
  reason: string
  conversations: number
  messages: number
}> {
  if (!hasDeviceToken()) {
    return { uploaded: false, reason: '还没有设备身份（联网后会自动建）', conversations: 0, messages: 0 }
  }

  options.onProgress?.('正在核对本机存档…')
  let summary
  try {
    summary = await detectLegacyLocal()
  } catch (err) {
    devWarn('[cloud] 读取本机存档失败:', err)
    return { uploaded: false, reason: '读取本机存档失败', conversations: 0, messages: 0 }
  }

  const marker = readUploadMarker()
  const decision = planUploadOnLogin({
    accountState: 'user',
    hasLocalData: !!summary?.hasData,
    localMessages: summary?.messages ?? 0,
    uploadedUserId: options.force ? null : (marker?.userId ?? null),
    currentUserId: options.userId,
  })
  if (!decision.run) {
    return { uploaded: false, reason: decision.reason, conversations: 0, messages: 0 }
  }

  options.onProgress?.('正在上传到服务器…')
  try {
    const outcome = await migrateLegacyLocal({ force: options.force })
    // skipped = 这台设备之前已经迁移过（老标记还在），这次什么都没传
    if (outcome.skipped) {
      // 也记一笔 marker：否则每次登录都会重新检测一遍本机存档、白跑一趟
      writeUploadMarker({
        at: Date.now(),
        userId: options.userId,
        conversations: 0,
        messages: 0,
      })
      return { uploaded: false, reason: '这台设备以前已经传过了', conversations: 0, messages: 0 }
    }
    const counts = {
      conversations: outcome.counts.conversations ?? 0,
      messages: outcome.counts.messages ?? 0,
    }
    writeUploadMarker({
      at: Date.now(),
      userId: options.userId,
      ...counts,
    })
    devInfo('[cloud] 本机存档已上传', `${counts.conversations} 段 / ${counts.messages} 条`)
    return { uploaded: true, reason: '', ...counts }
  } catch (err) {
    devWarn('[cloud] 上传失败:', err)
    return {
      uploaded: false,
      reason: err instanceof Error ? err.message : String(err),
      conversations: 0,
      messages: 0,
    }
  }
}
