// =============================================================================
// apiClient —— 新后端（FastAPI）的底层请求层
// -----------------------------------------------------------------------------
// 这一层只管四件事，别把业务塞进来：
//
//   1. **设备 token**：第一请求前自动 `POST /v1/auth/device` 拿一张匿名设备凭证，
//      存 localStorage，之后每个请求都带 `Authorization: Bearer <token>`。
//      （新后端的登录**不换 token**：登录只是把这张设备凭证挂到账号上，
//       所以这里不需要"登录后重新初始化"那一步。）
//   2. **统一错误**：后端所有错误都是 `{error:{code,message,details?}, request_id}`，
//      这里转成 ApiError（带 HTTP 状态 / 业务码 / retry_after），上层只认一种形状。
//   3. **401 自愈**：token 被服务端清掉（库重置、设备被删）时，丢掉旧 token 重新注册
//      一次并重放请求；只重放一次，避免死循环。
//   4. **可中止**：所有请求都透传 AbortSignal。
//
// 为什么单独一个文件而不是塞进 backend.ts：
//   登录（utils/auth.ts）和聊天（utils/backend.ts）用的是同一张设备凭证、同一套
//   错误结构。放一处才不会出现"聊天能连、登录 401"这种两套 token 的问题。
// =============================================================================

import { v1Url } from '../constants/apiRoutes'
import { APP_VERSION } from '../constants/popups'
import { devInfo, devWarn } from './logger'

/** 设备 token 的 localStorage key */
export const DEVICE_TOKEN_KEY = 'baker_device_token'

/** 设备信息（注册匿名设备时上报；后端只用于排查，不参与鉴权） */
export interface DeviceInfo {
  platform: string
  app_ver: string
  app_build: number
  model: string
  os_ver: string
  tz: string
  locale: string
}

/** 后端错误（`{error:{code,message,details}}`）的统一形状 */
export class ApiError extends Error {
  /** HTTP 状态码（网络层失败时为 0） */
  readonly status: number
  /** 业务错误码（英文、稳定），如 bad_credentials / no_conversation */
  readonly code: string
  /** 后端返回的附加信息（例如限流时的 retry_after） */
  readonly details: Record<string, unknown>

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }

  /** 建议等待秒数（限流/登录闸门会给；没给就是 0） */
  get retryAfter(): number {
    const raw = this.details.retry_after ?? this.details.retryAfter
    const value = typeof raw === 'number' ? raw : Number(raw)
    return Number.isFinite(value) && value > 0 ? value : 0
  }

  /** 是否值得原样重试（限流 / 上游临时失败 / 5xx） */
  get retryable(): boolean {
    return this.status === 429 || this.status === 502 || this.status === 503 || this.status === 504
  }
}

/** 内存里的 token 缓存（localStorage 才是权威，这里只是少读几次） */
let tokenCache: string | null = null

/** 正在进行中的设备注册（并发请求共用一次注册，避免注册出好几台"设备"） */
let registering: Promise<string> | null = null

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(DEVICE_TOKEN_KEY)
  } catch {
    return null
  }
}

function storeToken(token: string): void {
  tokenCache = token
  try {
    localStorage.setItem(DEVICE_TOKEN_KEY, token)
  } catch {
    // 隐私模式 / 存储被禁用：token 只存在内存里，本次会话内仍可用
  }
}

/** 丢弃当前设备凭证（登出、401 自愈、手动重置时用） */
export function forgetDeviceToken(): void {
  tokenCache = null
  try {
    localStorage.removeItem(DEVICE_TOKEN_KEY)
  } catch {
    // 静默失败
  }
}

/** 已经拿到设备 token 了吗（登录面板据此区分"还没初始化"） */
export function hasDeviceToken(): boolean {
  return !!readStoredToken()
}

/** 收集设备信息（只用于服务端排查；字段缺了也不影响注册） */
export function deviceInfo(): DeviceInfo {
  const nav = typeof navigator !== 'undefined' ? navigator : undefined
  const ua = nav?.userAgent ?? ''
  // ⚠ 后端 devices 表这几列都很窄（os_ver/平台/app_ver 都是 VARCHAR(32)，locale 16，
  //   tz 64）。以前这里直接塞整条 User-Agent（130+ 字符），MySQL 报 "Data too long"
  //   → 注册接口 500，表现为"进不去、一直在连接服务器"。这里逐项截短：
  //     · os_ver 只写人能看懂的短名字（Windows 10/11、Android 14…）
  //     · 其余按列宽截断，宁短勿长
  return {
    platform: detectPlatform(ua).slice(0, 32),
    app_ver: APP_VERSION.slice(0, 32),
    app_build: 0,
    model: '',
    os_ver: shortOs(ua).slice(0, 32),
    tz: timeZone().slice(0, 64),
    locale: (nav?.language ?? '').slice(0, 16),
  }
}

/**
 * User-Agent → 一句短的操作系统名
 *
 * 只用于服务端排查（"这台是哪类设备"），不需要精确到 build 号；
 * 后端那一列只有 32 字符，整条 UA 塞不进去。
 */
function shortOs(ua: string): string {
  if (!ua) return 'unknown'
  if (/Windows NT 10\.0/.test(ua)) return 'Windows 10/11'
  const windows = /Windows NT (\d+\.\d+)/.exec(ua)
  if (windows) return `Windows NT ${windows[1]}`
  const android = /Android\s+([\d.]+)/.exec(ua)
  if (android) return `Android ${android[1]}`
  const ios = /OS (\d+[_\d]*) like Mac/.exec(ua)
  if (ios) return `iOS ${ios[1].replace(/_/g, '.')}`
  if (/Mac OS X/.test(ua)) return 'macOS'
  if (/Linux/.test(ua)) return 'Linux'
  return 'unknown'
}

function detectPlatform(ua: string): string {
  if (/electron/i.test(ua)) return 'electron'
  const capacitor =
    typeof window !== 'undefined'
      ? (window as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor
      : undefined
  if (capacitor?.isNativePlatform?.()) return 'capacitor'
  if (/android/i.test(ua)) return 'android-web'
  if (/iphone|ipad|ipod/i.test(ua)) return 'ios-web'
  return 'web'
}

function timeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? ''
  } catch {
    return ''
  }
}

/**
 * 确保手里有一张设备凭证
 *
 * 并发调用共用同一个注册 Promise：两个请求同时发出时只会注册一台设备，
 * 否则第一台设备的会话会被第二台"看不见"。
 */
export async function ensureDeviceToken(signal?: AbortSignal): Promise<string> {
  const stored = tokenCache ?? readStoredToken()
  if (stored) {
    tokenCache = stored
    return stored
  }
  if (registering) return registering

  registering = (async () => {
    let response: Response
    try {
      response = await fetch(v1Url('/auth/device'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(deviceInfo()),
        signal,
      })
    } catch (err) {
      throw networkError(err)
    }
    if (!response.ok) throw await readError(response)
    const data = (await response.json()) as { device_id?: string; token?: string }
    if (!data.token) throw new ApiError(response.status, 'bad_response', '设备注册失败：服务端没有返回 token')
    storeToken(data.token)
    devInfo('[api] 已注册匿名设备', data.device_id ?? '')
    return data.token
  })()

  try {
    return await registering
  } finally {
    registering = null
  }
}

/** `apiFetch` 的可选项 */
export interface ApiOptions {
  /** 是否带设备凭证（只有 /auth/device 不需要） */
  auth?: boolean
  /** 401 时是否自动重新注册并重放（默认 true） */
  retryOn401?: boolean
  /** 已经拿到的 token（避免同一个流程里反复 await） */
  token?: string
}

/**
 * 请求新后端
 *
 * @param path   `/v1` 之后的路径（以 `/` 开头）
 * @param init   fetch 参数
 * @param opts   见 ApiOptions
 */
export async function apiFetch(
  path: string,
  init: RequestInit = {},
  opts: ApiOptions = {},
): Promise<Response> {
  const auth = opts.auth ?? true
  const headers = new Headers(init.headers)
  if (auth) {
    const token = opts.token ?? (await ensureDeviceToken(init.signal ?? undefined))
    headers.set('Authorization', `Bearer ${token}`)
  }

  let response: Response
  try {
    response = await fetch(v1Url(path), { ...init, headers })
  } catch (err) {
    throw networkError(err)
  }

  if (response.status === 401 && auth && (opts.retryOn401 ?? true)) {
    // token 失效（服务端重置/设备被删）：丢旧凭证、重新注册、重放一次
    devWarn('[api] token 失效，重新注册设备后重放请求', path)
    forgetDeviceToken()
    const token = await ensureDeviceToken(init.signal ?? undefined)
    headers.set('Authorization', `Bearer ${token}`)
    try {
      response = await fetch(v1Url(path), { ...init, headers })
    } catch (err) {
      throw networkError(err)
    }
  }

  return response
}

/** 解析错误响应为 ApiError */
export async function readError(response: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    // 非 JSON（网关 HTML 错误页 / 空响应）
  }
  const errorObj =
    body && typeof body === 'object' && 'error' in body
      ? (body as { error?: { code?: unknown; message?: unknown; details?: unknown } }).error
      : undefined
  const code = typeof errorObj?.code === 'string' ? errorObj.code : `http_${response.status}`
  const message =
    typeof errorObj?.message === 'string' && errorObj.message
      ? errorObj.message
      : response.status === 0
        ? '网络连接失败'
        : `请求失败（HTTP ${response.status}）`
  const details =
    errorObj?.details && typeof errorObj.details === 'object'
      ? (errorObj.details as Record<string, unknown>)
      : {}
  return new ApiError(response.status, code, message, details)
}

/** 网络层异常（fetch 抛 TypeError / AbortError）转成 ApiError */
function networkError(err: unknown): unknown {
  if (err instanceof DOMException && err.name === 'AbortError') return err
  if (err instanceof TypeError) {
    return new ApiError(0, 'network_error', '网络连接失败：无法连接到服务器，请检查网络后重试')
  }
  return err
}

/**
 * 请求并解析 JSON；非 2xx 一律抛 ApiError
 *
 * 大多数端点都是"成功返回 JSON / 失败抛错"，没有 204 的场景用它。
 * 204（登出、改密码）请直接用 apiFetch 判断 response.ok。
 */
export async function apiJson<T>(
  path: string,
  init: RequestInit = {},
  opts: ApiOptions = {},
): Promise<T> {
  const response = await apiFetch(path, init, opts)
  if (!response.ok) throw await readError(response)
  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError(response.status, 'bad_response', '服务端返回的不是有效 JSON')
  }
}

/** 把错误转成给玩家看的一句话（上层 UI 直接用） */
export function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return err.message
    if (err.code === 'bad_credentials') return '用户名或密码不对'
    if (err.code === 'username_taken') return '这个用户名已经有人用了，换一个吧'
    if (err.code === 'device_taken') return '这台设备已经登录了另一个账号，请先登出'
    if (err.code === 'account_disabled') return '这个账号已被停用'
    if (err.code === 'too_many_attempts') {
      const wait = Math.max(1, Math.round(err.retryAfter))
      return `尝试次数太多了，约 ${wait} 秒后再试`
    }
    if (err.code === 'bad_claim_code') {
      // 注册时带了取件码、但码无效/过期：后端会把整次注册回滚（账号没创建）。
      // 文案必须说清"这次没注册成功"，否则用户会以为自己已经注册过了。
      return '取件码无效或已过期（24 小时有效）：这次没注册成功（账号未创建）。换个取件码，或者去掉取件码重新注册'
    }
    if (err.code === 'weak_password') return err.message
    if (err.status === 429) {
      const wait = Math.max(1, Math.round(err.retryAfter))
      return wait > 0 ? `服务繁忙（限速），约 ${wait} 秒后再试` : '服务繁忙（限速），请稍后重试'
    }
    return err.message
  }
  if (err instanceof DOMException && err.name === 'AbortError') return '已取消'
  return err instanceof Error ? err.message : String(err)
}
