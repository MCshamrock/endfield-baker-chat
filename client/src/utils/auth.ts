// =============================================================================
// auth —— 账号接口（后端 app/api/auth.py，docs/03-登录与身份.md）
// -----------------------------------------------------------------------------
// 一句话理解这套登录：
//
//   **登录不换 token。** 客户端手里那张匿名设备凭证继续用，登录只是把这张设备
//   "挂到账号上"；服务端把 `conversations.user_id` 从 NULL 改成账号 id 就完了。
//   所以这里没有 "登录后重新初始化" / "重新拉会话" 这些步骤。
//
// 六个端点：
//   POST /v1/auth/device     匿名设备（由 utils/apiClient 自动完成）
//   POST /v1/auth/register   用户名+密码注册（注册即登录）
//   POST /v1/auth/login      用户名+密码登录
//   POST /v1/auth/logout     退出（只摘设备，数据留在账号里）
//   GET  /v1/auth/me         我是谁：guest / user + 名下设备
//   POST /v1/auth/password   改密码（会踢掉其它设备）
//
// 前端要守的三条（照 docs/03 §6）：
//   1. 启动时调 me 决定显示"登录"还是昵称；
//   2. 登录/注册成功后不重拉会话，只在 adopted_conversations > 0 时提示一句；
//   3. 登出后本地缓存要清（本项目的"本地缓存"是本地卡库，属于玩家自己的存档，
//      不清 —— 见 docs 里的说明；这里只清服务端可见性）。
// =============================================================================

import { apiFetch, apiJson, readError } from './apiClient'

/** 账号对外的形状（服务端绝不返回密码哈希） */
export interface AccountUser {
  id: string
  username: string
  display_name: string
  created_at: string
}

/** 账号名下的一台设备 */
export interface AccountDevice {
  id: string
  platform: string
  app_ver: string
  last_seen_at: string | null
}

/** `GET /v1/auth/me`：访客或已登录账号 */
export interface AccountMe {
  kind: 'guest' | 'user'
  user?: AccountUser | null
  devices: AccountDevice[]
}

/** 登录 / 注册结果 */
export interface AuthResult {
  user: AccountUser
  /** 这次登录把多少条"登录前在这台设备上聊的"划到了账号名下 */
  adopted_conversations: number
}

/**
 * `GET /v1/auth/owner-data`：这份身份名下有没有聊天记录（只有数量）
 *
 * 用途只有一个：**有记录却没登录账号**时，先把人拦下来登录/注册。
 * 因为那批记录此刻挂在"这台设备"名下，清缓存 / 换浏览器就跟丢了。
 */
export interface OwnerData {
  conversations: number
  messages: number
}

/** 问服务端"这份身份名下有多少记录"（只读，不改任何东西） */
export async function fetchOwnerData(signal?: AbortSignal): Promise<OwnerData> {
  return apiJson<OwnerData>('/auth/owner-data', { method: 'GET' }, { retryOn401: true })
}

// ---------------------------------------------------------------------------
// 前后端必须一致的用户名 / 密码规则（照抄 app/repos/users.py、app/passwords.py）
// 前端只是提前拦一次，最终判定仍在服务端 —— 两边文案也要一致。
// ---------------------------------------------------------------------------

/** 用户名长度下限 */
export const USERNAME_MIN = 2
/** 用户名长度上限 */
export const USERNAME_MAX = 20
/** 首位中文/字母，其余中文/字母/数字/下划线 */
export const USERNAME_PATTERN = /^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{1,19}$/

/** 密码长度（只要求长度，不强制复杂度） */
export const PASSWORD_MIN = 8
export const PASSWORD_MAX = 128

/** 用户名规则的一句话说明（与后端 400 文案同一句） */
export const USERNAME_RULE_TEXT =
  `用户名要 ${USERNAME_MIN}–${USERNAME_MAX} 个字符，只能用中文、字母、数字、下划线，` +
  '且必须以中文或字母开头'

/** 密码规则的一句话说明 */
export const PASSWORD_RULE_TEXT = `密码长度要在 ${PASSWORD_MIN}–${PASSWORD_MAX} 个字符之间`

/** 校验用户名；合法返回空串，不合法返回给人看的原因 */
export function validateUsername(raw: string): string {
  const name = raw.trim()
  if (!name) return '请填写用户名'
  if (!USERNAME_PATTERN.test(name)) return USERNAME_RULE_TEXT
  return ''
}

/** 校验密码；合法返回空串 */
export function validatePassword(raw: string, label = '密码'): string {
  if (!raw) return `请填写${label}`
  if (raw.length < PASSWORD_MIN || raw.length > PASSWORD_MAX) return PASSWORD_RULE_TEXT
  return ''
}

// ---------------------------------------------------------------------------
// 接口
// ---------------------------------------------------------------------------

/** 我现在是谁。401 不会发生（设备凭证由 apiClient 自动处理） */
export async function fetchMe(signal?: AbortSignal): Promise<AccountMe> {
  return apiJson<AccountMe>('/auth/me', { method: 'GET' }, { retryOn401: true })
}

/**
 * 注册（注册即登录）
 *
 * 用户名允许中文、大小写不敏感（判重交给数据库的 `_ai_ci` 排序规则）。
 * 返回的 `adopted_conversations` 是这次收编的会话数。
 *
 * `claimCode`（可选）：在别的设备「一键上传数据」拿到的 8 位取件码。
 * 带了它，后端会在**创建账号的同时**把那份包领到这个账号名下 ——
 * 所以注册请求失败（码无效/过期，`bad_claim_code`）时整次注册会回滚，
 * 账号不会留下半个。前端要做的就是提示"这次没注册成功，换个码或去掉码重试"。
 */
export async function registerAccount(
  username: string,
  password: string,
  displayName = '',
  claimCode = '',
  signal?: AbortSignal,
): Promise<AuthResult> {
  const payload: Record<string, unknown> = {
    username: username.trim(),
    password,
    display_name: displayName.trim(),
  }
  // 只在真的填了码时才带这个字段：空串会变成"带了个空码"，后端无法区分
  // "没填"和"填错了"，会把一次正常注册也回滚掉。
  const code = (claimCode || '').trim().toUpperCase()
  if (code) payload.claim_code = code
  return apiJson<AuthResult>('/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
}

/** 登录（账号不存在与密码错返回同一个 401，前端不要尝试区分） */
export async function loginAccount(
  username: string,
  password: string,
  signal?: AbortSignal,
): Promise<AuthResult> {
  return apiJson<AuthResult>(
    '/auth/login',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.trim(), password }),
      signal,
    },
  )
}

/** 退出账号（204；服务端只把设备从账号上摘下来，数据一条不动） */
export async function logoutAccount(signal?: AbortSignal): Promise<void> {
  const response = await apiFetch('/auth/logout', { method: 'POST', signal })
  if (!response.ok) throw await readError(response)
}

/** 改密码（204；成功后服务端会摘掉其它设备，自己这台不用重新登录） */
export async function changePassword(
  oldPassword: string,
  newPassword: string,
  signal?: AbortSignal,
): Promise<void> {
  const response = await apiFetch('/auth/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    signal,
  })
  if (!response.ok) throw await readError(response)
}
