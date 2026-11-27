// =============================================================================
// apiRoutes —— 后端地址（新后端 web/back，FastAPI）
// -----------------------------------------------------------------------------
// 旧版这里是「默认域名 / 备用域名」两条路线（api.peilika.beer /
// api2.peilika.beer，网关 5501 + 群聊 5810）。新后端是**一个**服务：
//
//   {base}/v1/*     新契约（设备 token、会话、消息、附件、推荐……）
//   {base}/group/*  老群聊协议（新后端原样兼容，见 docs/06-老群聊协议.md）
//
// 因此这里只回答一个问题：**后端根地址是什么**。
//
// 解析顺序（先到先得）：
//   1. localStorage `baker_backend_url`  —— 调试/换机用，不占界面
//   2. 构建期注入 `VITE_BAKER_BACKEND`    —— 打包时写死一个地址
//   3. file:// 打开的本地构建              —— 退回 http://127.0.0.1:5660
//   4. 其余（同源部署 / vite 代理）        —— 空串，即相对路径
//
// 生产部署推荐**同源**：前端静态文件由同一个域名（nginx / 后端静态挂载）提供，
// 这样空串就是对的，既没有跨域 cookie 问题，也不用在构建时写死地址。
//
// 设置界面里的「默认 / 备用」两个按钮保留（UI 不改动），两条现在指向同一个
// 新后端 —— 它们显示的是"当前到底打到哪了"，切换不再改变结果。
// =============================================================================

/** localStorage：后端根地址覆盖（调试用） */
export const BACKEND_URL_OVERRIDE_KEY = 'baker_backend_url'

/** 构建期注入的后端根地址（可选） */
const BUILD_BACKEND = (import.meta.env.VITE_BAKER_BACKEND as string | undefined) ?? ''

/** 本地 file:// 打开构建时的默认后端（Windows 客户端/双击 dist/index.html） */
const LOCAL_BACKEND = 'http://127.0.0.1:5660'

/**
 * 规范化用户填的地址
 *
 * 允许用户把 `/v1`、`/chat`、`/group` 或末尾斜杠一起粘进来 ——
 * 这些是最容易多写的三段路径，粘错不该表现为"所有请求 404"。
 */
export function normalizeBase(raw: string): string {
  let base = (raw || '').trim()
  if (!base) return ''
  base = base.replace(/\/+$/, '')
  base = base.replace(/\/(v1|chat|group)$/i, '')
  return base.replace(/\/+$/, '')
}

/** 当前后端根地址（不带末尾斜杠；空串 = 同源） */
export function backendBaseUrl(): string {
  try {
    const override = normalizeBase(localStorage.getItem(BACKEND_URL_OVERRIDE_KEY) || '')
    if (override) return override
  } catch {
    // localStorage 被禁用（隐私模式）：继续走构建期/默认值
  }
  const build = normalizeBase(BUILD_BACKEND)
  if (build) return build
  try {
    if (typeof location !== 'undefined' && location.protocol === 'file:') return LOCAL_BACKEND
  } catch {
    // 没有 location（极少见的非浏览器环境）：按同源处理
  }
  return ''
}

/** 新契约地址：`/v1` + path（path 以 `/` 开头） */
export function v1Url(path: string): string {
  return `${backendBaseUrl()}/v1${path}`
}

/** 老群聊协议地址：`/group` + path */
export function groupUrl(path: string): string {
  return `${backendBaseUrl()}/group${path}`
}

/** 设置覆盖（调试入口；界面没暴露，需要时在控制台调） */
export function setBackendUrlOverride(url: string): void {
  try {
    const normalized = normalizeBase(url)
    if (normalized) localStorage.setItem(BACKEND_URL_OVERRIDE_KEY, normalized)
    else localStorage.removeItem(BACKEND_URL_OVERRIDE_KEY)
  } catch {
    // 静默失败：不影响本次运行
  }
}

// ---------------------------------------------------------------------------
// 以下为旧接口（设置界面 / settings store 仍在引用），保留同名导出
// ---------------------------------------------------------------------------

/** localStorage key：旧版的"后端路线"选择。现在只影响显示，不影响请求。 */
export const API_ROUTE_KEY = 'baker_api_route'

/** 路线标识（保留旧名字，避免设置界面改代码） */
export type ApiRoute = 'primary' | 'backup'

/**
 * 两条路线现在的地址
 *
 * 用 getter 而不是常量：localStorage 里的覆盖可能在本模块加载之后才写入。
 * （SettingsDialog 在模块初始化时会读一次，读到的是那一刻的地址。）
 */
export const API_BASES: Record<ApiRoute, string> = {
  get primary(): string {
    return backendBaseUrl()
  },
  get backup(): string {
    return backendBaseUrl()
  },
}

/** 路线显示名（设置界面用） */
export const API_ROUTE_LABELS: Record<ApiRoute, string> = {
  primary: '默认',
  backup: '备用',
}

/** 当前路线（保留旧接口；两条路线地址相同） */
export function currentApiRoute(): ApiRoute {
  try {
    return localStorage.getItem(API_ROUTE_KEY) === 'backup' ? 'backup' : 'primary'
  } catch {
    return 'primary'
  }
}

/** 当前后端根地址（旧名字） */
export function apiBase(): string {
  return backendBaseUrl()
}

/**
 * 旧名字：单聊网关
 *
 * 旧代码把它当"到 /chat 为止的完整地址"用，新代码只把它当根地址 + `/v1`；
 * 需要拼具体端点时请用 v1Url()。
 */
export function gatewayUrl(): string {
  return `${backendBaseUrl()}/v1`
}

/** 旧名字：群聊服务（新后端仍提供 /group/*） */
export function groupBaseUrl(): string {
  return `${backendBaseUrl()}/group`
}
