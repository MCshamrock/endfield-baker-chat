/**
 * api.ts —— 面板唯一的网络出口。
 *
 * 为什么只留一个出口：
 *   后台有 8 个页面、十几个请求。如果每处自己 `fetch`，那么"带 token""401 怎么办"
 *   "错误体怎么读"就会各写一遍，迟早有一处忘了带 token，或者把 401 当成普通错误
 *   弹一句英文。这里把这些决定收在一处，页面只拿到"数据"或"一个带中文话的错"。
 *
 * 后端的约定（照 `web/back/app/errors.py`）：
 *   * 所有业务错误都是 `{"error": {"code", "message"}}`，message 是给人看的中文；
 *   * 缺 token / token 无效 = 401（要回登录页）；
 *   * 登录了但不是管理员 = 403（**不要**回登录页，要说清"没权限"）。
 *
 * 鉴权方式容易误会，写清楚：
 *   这个后端没有"登录发一个 token"这回事。token 属于**设备**：先 `POST /v1/auth/device`
 *   拿一张设备 token，再 `POST /v1/auth/login` 把这张 token 挂到账号上。
 *   所以登录 = 注册设备（一次）+ 绑定账号。面板把设备 token 存在 localStorage，
 *   下次打开直接拿它打后台接口，还在就是管理员，401 才回登录页。
 */

import type {
  AdminCharacter,
  AdminKey,
  AdminUser,
  CharacterPatch,
  ConfigItem,
  KeyStats,
  LoginResult,
  Overview,
  ReasoningResult,
  UsageDay,
  UserPage,
  UserPatch,
} from './types.ts'

/** 设备元信息里报的面板版本号。排障时能看出"是哪个前端干的"。 */
export const PANEL_APP_VER = 'panel/0.1.0'

/** token 存哪儿。抽成接口是为了让 Node 下的冒烟脚本能用同一套代码（不碰 localStorage）。 */
export interface TokenStore {
  get(): string | null
  set(token: string): void
  clear(): void
}

/** 浏览器里的实现：localStorage 可能被隐私模式禁掉，所以全部 try 包住。 */
export const browserTokenStore: TokenStore = {
  get() {
    try {
      return localStorage.getItem('baker.panel.token')
    } catch {
      return null
    }
  },
  set(token: string) {
    try {
      localStorage.setItem('baker.panel.token', token)
    } catch {
      /* 存不下就算了：本次会话还能用，只是刷新后要重登 */
    }
  },
  clear() {
    try {
      localStorage.removeItem('baker.panel.token')
    } catch {
      /* 同上 */
    }
  },
}

/** 内存实现：给冒烟脚本用，也给"localStorage 写不进去"时兜底 */
export function memoryTokenStore(initial: string | null = null): TokenStore {
  let token = initial
  return {
    get: () => token,
    set: (next) => {
      token = next
    },
    clear: () => {
      token = null
    },
  }
}

/** 后端给的结构化错误。页面捕它，拿 message 直接显示（本来就是中文人话）。 */
export class ApiError extends Error {
  /** HTTP 状态；0 表示请求根本没发出去（断网、后端没起） */
  readonly status: number
  /** 后端错误码，英文、稳定（`bad_credentials` / `forbidden` / …） */
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** 是否"这个人不是管理员"。页面用它决定显示"无权限"而不是普通报错。 */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** 是否"身份没了"。触发回登录页。 */
  get isUnauthorized(): boolean {
    return this.status === 401
  }
}

export interface ApiClientOptions {
  /** 空 = 同源（dev 走 vite 代理，生产走 nginx 反代） */
  baseUrl?: string
  tokenStore?: TokenStore
  /** 401 时回调：页面在这里清状态、回登录页。只在这一个地方触发。 */
  onUnauthorized?: (error: ApiError) => void
  /** 换掉 fetch 实现（冒烟脚本用来指向假后端；一般不用传） */
  fetchImpl?: typeof fetch
}

type Query = Record<string, string | number | boolean | null | undefined>

interface RequestOptions {
  body?: unknown
  query?: Query
  /** 401 时是否触发 onUnauthorized。设备注册这种"本来就没身份"的请求关掉它 */
  authRequired?: boolean
}

/** 状态码兜底文案：后端没给 message 时也不能弹一句英文或空白。 */
function fallbackMessage(status: number): string {
  if (status === 400) return '请求不合法，检查一下填的内容'
  if (status === 401) return '登录已失效，请重新登录'
  if (status === 403) return '这个账号没有管理员权限'
  if (status === 404) return '没有找到这条数据'
  if (status === 409) return '和现有数据冲突了'
  if (status === 422) return '字段格式不对，检查一下再提交'
  if (status === 429) return '操作太频繁，等一下再试'
  if (status >= 500) return '后端出错了，稍后再试'
  return `请求失败（HTTP ${status}）`
}

function buildUrl(baseUrl: string, path: string, query?: Query): string {
  const url = `${baseUrl}${path}`
  if (!query) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    // 空串和 undefined 一律不带：后端把 `q=` 当空搜索，带了也没意义，
    // 但 `limit=undefined` 会变成一个字符串 "undefined" 直接 422。
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const qs = search.toString()
  return qs ? `${url}?${qs}` : url
}

export function createApiClient(options: ApiClientOptions = {}) {
  const baseUrl = options.baseUrl ?? ''
  const tokenStore = options.tokenStore ?? browserTokenStore
  const call: typeof fetch = (...args) => (options.fetchImpl ?? fetch)(...args)

  /**
   * 所有请求的唯一入口：拼 URL、带 token、解析错误体、分发 401。
   * 返回的是已经 `JSON.parse` 过的对象（204 返回 undefined）。
   */
  async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json'

    let response: Response
    try {
      response = await call(buildUrl(baseUrl, path, opts.query), {
        method,
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      })
    } catch {
      // 连不上是常态（后端没起、隧道断了），给人一句能动手的话
      throw new ApiError(0, 'network_error', '连不上后端服务，检查网络或者后端是否在跑')
    }

    // 204 / 空体：没有内容可解析
    if (response.status === 204) return undefined as T

    const text = await response.text()
    let parsed: unknown = undefined
    if (text) {
      try {
        parsed = JSON.parse(text)
      } catch {
        // 反代返回了 HTML 错误页（502 之类）时走到这里
        if (response.ok) {
          throw new ApiError(response.status, 'bad_response', '后端返回的不是 JSON，检查反代配置')
        }
      }
    }

    if (!response.ok) {
      const envelope = parsed as { error?: { code?: string; message?: string } } | undefined
      const code = envelope?.error?.code ?? `http_${response.status}`
      const message = envelope?.error?.message ?? fallbackMessage(response.status)
      const error = new ApiError(response.status, code, message)
      // 401 只有一个去处：登录页。这里统一喊一声，页面不用每个请求都判断。
      if (response.status === 401 && opts.authRequired !== false) {
        options.onUnauthorized?.(error)
      }
      throw error
    }

    return parsed as T
  }

  return {
    /** 当前 token（不打印、不落 console —— 只给内部判断用） */
    hasToken(): boolean {
      return Boolean(tokenStore.get())
    },

    /** 丢掉本地设备 token。收到 401 时必须调，否则下次登录会拿死 token 再撞 401。 */
    clearToken(): void {
      tokenStore.clear()
    },

    /**
     * 确保手里有一张设备 token。已经有了就复用：
     * 重复注册会把"我的设备"列表刷成一堆僵尸设备，而且旧 token 里的数据就看不见了。
     */
    async ensureDevice(): Promise<string> {
      const existing = tokenStore.get()
      if (existing) return existing
      const out = await request<{ device_id: string; token: string }>('POST', '/v1/auth/device', {
        body: {
          platform: 'web-panel',
          app_ver: PANEL_APP_VER,
          model: typeof navigator === 'undefined' ? 'node' : navigator.userAgent.slice(0, 64),
          tz: typeof Intl === 'undefined' ? '' : String(Intl.DateTimeFormat().resolvedOptions().timeZone),
          locale: typeof navigator === 'undefined' ? '' : navigator.language,
        },
        authRequired: false,
      })
      tokenStore.set(out.token)
      return out.token
    },

    /**
     * 登录 = 拿设备 token + 绑定账号。
     * 返回的 user **没有 role**，所以"是不是管理员"要另外打一次后台接口试（见 App.vue）。
     */
    async login(username: string, password: string): Promise<LoginResult['user']> {
      await this.ensureDevice()
      const doLogin = () =>
        request<LoginResult>('POST', '/v1/auth/login', {
          body: { username, password },
          authRequired: false, // 登录失败是 401，但这时不该"跳登录页"（我们本来就在登录页）
        })
      try {
        return (await doLogin()).user
      } catch (err) {
        // `bad_token` = 本地存着的设备 token 已经作废（账号被删/设备被清/服务端换库）。
        // 这时不能直接报"密码错"：先丢掉死 token、重新注册一台设备、再登一次。
        if (err instanceof ApiError && err.code === 'bad_token') {
          tokenStore.clear()
          await this.ensureDevice()
          return (await doLogin()).user
        }
        throw err
      }
    },

    /** 退出：把设备从账号上摘下来（后端不删数据），然后清掉本地 token。 */
    async logout(): Promise<void> {
      try {
        if (tokenStore.get()) {
          await request<void>('POST', '/v1/auth/logout', { authRequired: false })
        }
      } catch {
        // 退不干净也要清本地：面板宁可让用户重登，也不要卡在"退不出去"的状态
      } finally {
        tokenStore.clear()
      }
    },

    // ------------------------------------------------------------ 后台数据
    overview: () => request<Overview>('GET', '/v1/admin/overview'),

    listUsers: (params: { q?: string; limit?: number; offset?: number } = {}) =>
      request<UserPage>('GET', '/v1/admin/users', { query: { ...params } }),

    patchUser: (id: string, patch: UserPatch) =>
      request<AdminUser>('PATCH', `/v1/admin/users/${encodeURIComponent(id)}`, { body: patch }),

    listCharacters: () => request<{ items: AdminCharacter[] }>('GET', '/v1/admin/characters'),

    patchCharacter: (slug: string, patch: CharacterPatch) =>
      request<{ id: string; name: string; enabled: boolean; active_weight: number; sort_order: number }>(
        'PATCH',
        `/v1/admin/characters/${encodeURIComponent(slug)}`,
        { body: patch },
      ),

    listConfig: () => request<{ items: ConfigItem[] }>('GET', '/v1/admin/config'),

    putConfig: (key: string, value: string) =>
      request<ConfigItem>('PUT', `/v1/admin/config/${encodeURIComponent(key)}`, { body: { value } }),

    listKeys: () => request<{ items: AdminKey[] }>('GET', '/v1/admin/keys'),

    patchKey: (id: number, enabled: boolean) =>
      request<{ id: number; label: string; enabled: boolean }>('PATCH', `/v1/admin/keys/${id}`, {
        body: { enabled },
      }),

    usage: (days: 7 | 30 | 90) => request<{ days: number; items: UsageDay[] }>('GET', '/v1/admin/usage', { query: { days } }),

    reasoning: (publicId: string, limit = 20) =>
      request<ReasoningResult>('GET', `/v1/admin/conversations/${encodeURIComponent(publicId)}/reasoning`, {
        query: { limit },
      }),

    /** 密钥池汇总（首页那块和 overview 一起用，单独导出方便页面复用形状） */
    keyStatsOf: (keys: AdminKey[]): KeyStats => {
      const enabled = keys.filter((k) => k.enabled).length
      return {
        total: keys.length,
        enabled,
        cooldown: keys.filter((k) => k.enabled && k.cooling).length,
        disabled: keys.length - enabled,
      }
    },
  }
}

export type ApiClient = ReturnType<typeof createApiClient>

/**
 * 401 的唯一去处。
 *
 * 为什么用模块级变量而不是构造时传进去：`api` 这个实例在模块加载时就要建好
 * （各页面直接 import 它），而"回登录页"要等 App.vue 挂载后才知道怎么做。
 * 闭包读变量、变量后赋值，两边都不用互相等。App.vue 启动时调一次
 * `attachUnauthorizedHandler` 就够了。
 */
let unauthorizedHandler: ((error: ApiError) => void) | null = null

export function attachUnauthorizedHandler(handler: (error: ApiError) => void) {
  unauthorizedHandler = handler
}

/** 页面用的那一个实例：同源 + localStorage 存 token + 401 统一回调 */
export const api = createApiClient({
  onUnauthorized: (error) => unauthorizedHandler?.(error),
})
