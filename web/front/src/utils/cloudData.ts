// =============================================================================
// 云同步（取件码中转）—— utils/cloudData.ts
// -----------------------------------------------------------------------------
// 干什么（2026-11-18 起改接到我们自己的新后端 web/back，不再是 local.peilika.beer）：
//   · uploadCloudData(json)   上传数据包  POST /v1/uploads        → 8 位取件码
//   · verifyUploadCode(code)  验码        GET  /v1/uploads/{code} → 还在不在、领过几次
//   · claimUploadCode(code)   领取        POST /v1/uploads/claim  → 导入结果（要登录）
//   · claimAfterVerify(code)  上面两步合起来用（界面就是"先验码再领取"）
//
// 为什么分三个接口：
//   **上传是匿名的，领取要登录**。上传谁都能传（服务器只认 24 小时），
//   领取要把这批会话的归属写到账号 / 设备上，所以必须带设备 token。
//   「注册时填码」那条路是后端在 /auth/register 里顺手做的（字段 claim_code），
//   前端只需要把码一起发过去，见 utils/auth.ts。
//
// 为什么上传还留着 XMLHttpRequest：
//   fetch **拿不到上传进度**（没有 upload.onprogress）。XHR 能拿到真实字节数与速度，
//   界面才能画出进度条。没有 XHR 的环境（Node 里跑自测脚本）退回 fetch ——
//   此时给不出百分比，只能显示"正在上传"。
//
// 服务端规则（同事实现，前端照抄，别自己改形状）：
//   · 取件码 8 位，24 小时后失效
//   · 一份包可以被**多台设备重复领取**；同一设备重复领同一份包，
//     后端按内容哈希去重并回 `duplicate: true`
//   · 上传上限 64MB；同一 IP 每小时最多 5 次
// =============================================================================

import { v1Url } from '../constants/apiRoutes'
import { ApiError, apiJson } from './apiClient'

/** 取件码长度（服务端固定 8 位；字母数字） */
export const CLAIM_CODE_LEN = 8

/** 上传成功后的返回（POST /v1/uploads 的 201 body） */
export interface CloudUploadResult {
  /** 取件码（给用户在别的设备上输入 / 注册时填写） */
  code: string
  /** 过期时刻（ISO 字符串，UTC 带 +00:00） */
  expires_at: string
  /** 服务器实际存下来的字节数 */
  bytes: number
  /** 服务端判断的包类型：json / zip */
  source: string
}

/** 验码返回（GET /v1/uploads/{code}） */
export interface CloudUploadInfo {
  code: string
  bytes: number
  source: string
  expires_at: string
  /** 是否已经被领过（领取次数 > 0） */
  claimed: boolean
  /** 被领过几次（多台设备各领一次就 +1） */
  claim_count: number
}

/**
 * 领取返回：与 POST /v1/imports 同一个形状
 *
 * 所以"领包"和"导入本地导出的包"在服务端是同一件事，只是数据来源不同
 * （一个来自取件码，一个来自直接上传）。
 */
export interface CloudClaimResult {
  id: string
  /** true = 这份包这台设备之前领过，后端按内容哈希去重了（没重复导入） */
  duplicate: boolean
  source: string
  mode: string
  package_version: number | string
  cards: number
  conversations: number
  messages: number
  images: number
  skipped: number
  notes: string[]
  created_at: string
  /** 撤销时间（刚导入时是 null） */
  revoked_at?: string | null
}

/** 进度回调数据 */
export interface CloudProgress {
  /** 当前阶段：上传 / 下载（下载现在只用于"领取"，没有大文件要传，一般不会有进度帧） */
  phase: 'upload' | 'download'
  /** 已传输字节 */
  loaded: number
  /** 总字节（0 = 服务器没给 Content-Length，无法算百分比） */
  total: number
  /** 百分比 0~100；total 未知时为 -1 */
  percent: number
  /** 瞬时速度（字节/秒，滑动窗口 + 平滑） */
  speedBps: number
  /** 已耗时（毫秒） */
  elapsedMs: number
  /** 预计剩余（毫秒）；算不出来时为 null */
  etaMs: number | null
  /**
   * 字节已传完、正在**等服务器处理**
   *
   * 为什么需要：上传的字节跑完后，服务器还要解包、写库（大包会等几秒到几十秒），
   * 此时若直接报 100%，界面看起来"传完了却卡着不动"，用户以为坏了。
   * 置 true 后 UI 显示"已上传，正在等待服务器处理…"并走不确定动画。
   */
  waitingServer?: boolean
}

export type CloudProgressFn = (p: CloudProgress) => void

/** 超时：大包上传给 5 分钟（进度条会显示实时速度，卡住一眼能看出来） */
const TIMEOUT_BULK_MS = 300_000

// ---------------------------------------------------------------------------
// 取件码的清洗与校验
// ---------------------------------------------------------------------------

/**
 * 清洗取件码
 *
 * 用户在别的设备上看到 8 位码，抄的时候常带空格/小写/全角字符。
 * 这里统一：转大写、只留 A–Z 与 0–9、截到 8 位。
 * 注意"截断"只发生在**提交/失焦**时；输入过程中保留非法字符，
 * 由输入框自己标红（否则用户永远看不到"你打的这个字符不合法"）。
 */
export function normalizeClaimCode(raw: string): string {
  return (raw || '')
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '')
    .slice(0, CLAIM_CODE_LEN)
}

/** 是否是合法的取件码：**恰好** 8 位字母数字（多一位少一位都不算） */
export function isClaimCodeValid(raw: string): boolean {
  const cleaned = (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '')
  return cleaned.length === CLAIM_CODE_LEN
}

// ---------------------------------------------------------------------------
// 小工具：字节 / 速度 / 时长 / 时间 的显示
// ---------------------------------------------------------------------------

/** 字节 → 人类可读（云同步区块显示用） */
export function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`
}

/** 速度 → 人类可读 */
export function formatSpeed(bps: number): string {
  return bps > 0 ? `${formatBytes(bps)}/s` : '—'
}

/** 毫秒 → "12s" / "1分23秒" */
export function formatDuration(ms: number | null): string {
  if (ms === null || !isFinite(ms) || ms < 0) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}分${String(s % 60).padStart(2, '0')}秒`
}

/**
 * 过期时刻 → 本地时间字符串
 *
 * 后端给的是 ISO（带 +00:00）。直接原样显示会让用户拿 UTC 当本地时间对不上；
 * 这里转成本地时区的「2026-11-19 08:00」。
 */
export function formatExpiresAt(iso: string): string {
  const t = new Date(iso)
  if (!iso || Number.isNaN(t.getTime())) return iso || '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${pad(t.getMonth() + 1)}-${pad(t.getDate())} ${pad(t.getHours())}:${pad(t.getMinutes())}`
}

/** 等待秒数 → 人话（限流提示里用） */
function formatWait(seconds: number): string {
  const s = Math.max(1, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.ceil(s / 60)} 分钟`
  return `${Math.ceil(s / 3600)} 小时`
}

// ---------------------------------------------------------------------------
// 错误 → 人话
// ---------------------------------------------------------------------------

/** 这次请求属于哪一步（不同步骤对同一个状态码的解释不一样） */
type CloudAction = 'upload' | 'verify' | 'claim'

/** 后端错误体（`{error:{code,message,details}}`）里我们关心的几项 */
interface BackendErrorBody {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
    retry_after?: number
  }
  retry_after?: number
}

/** 从响应原文里尽量抠出 code / message / retry_after */
function readBackendError(rawText: string): {
  code: string
  message: string
  retryAfter: number
} {
  let body: BackendErrorBody | null = null
  try {
    body = JSON.parse(rawText) as BackendErrorBody
  } catch {
    // 网关 HTML 错误页 / 空 body：按没有结构化错误处理
  }
  const error = body?.error
  const details = error?.details ?? {}
  const rawRetry =
    details.retry_after ?? details.retryAfter ?? error?.retry_after ?? body?.retry_after
  const retryValue = typeof rawRetry === 'number' ? rawRetry : Number(rawRetry)
  return {
    code: typeof error?.code === 'string' ? error.code : '',
    message: typeof error?.message === 'string' ? error.message : '',
    retryAfter: Number.isFinite(retryValue) && retryValue > 0 ? retryValue : 0,
  }
}

/**
 * 把 HTTP 状态码翻成一句"用户能照着做"的中文
 *
 * 为什么不用后端的 message：错误码是稳定的（404/410/413/429/507），
 * 文案要能提示下一步动作（换码 / 明天再传 / 删图再传），这些只有前端知道
 * 该怎么说才贴合界面。后端 message 只在没有对应状态码时兜底。
 */
function cloudErrorText(
  action: CloudAction,
  status: number,
  rawText = '',
  fallbackMessage = '',
  retryAfterHint = 0,
): string {
  const parsed = readBackendError(rawText)
  const retryAfter = retryAfterHint || parsed.retryAfter
  if (status === 0) return '连不上服务器：请检查网络后重试'
  if (status === 413) {
    return '数据太大了：服务器单份上限 64MB。先在「数据管理」里删掉一些图片，或用「导出数据」的 ZIP 包再传一次'
  }
  if (status === 507) return '服务器存储满了，暂时不能上传；请过一会儿再试'
  if (status === 429) {
    const wait = retryAfter > 0 ? `，约 ${formatWait(retryAfter)}后再试` : ''
    return action === 'upload'
      ? `上传太频繁了（同一网络每小时最多 5 次）${wait}`
      : `操作太频繁了${wait}`
  }
  if (status === 401) {
    return '这个操作要先登录：去「设置 → 账号」登录，或在注册时把取件码填进「取件码（可选）」'
  }
  if (status === 404) {
    return '取件码不存在：请核对一下有没有输错（码里只有大写字母和数字，注意别把 0 打成 O）'
  }
  if (status === 410) {
    return '这个取件码已经过期了（有效期 24 小时）：请回原来那台设备重新「一键上传数据」拿新码'
  }
  if (status === 400) {
    return action === 'upload'
      ? '服务器读不出这个包：请用「导出 JSON」或「导出数据」生成的文件再试'
      : parsed.message || '请求不合法，请重试'
  }
  return fallbackMessage || parsed.message || `请求失败（HTTP ${status}）`
}

// ---------------------------------------------------------------------------
// 上传
// ---------------------------------------------------------------------------

/** 速度计：滑动窗口（默认最近 1 秒）+ 指数平滑，避免数字乱跳 */
function makeSpeedMeter(windowMs = 1000) {
  const samples: Array<[number, number]> = []
  let smoothed = 0
  return (loaded: number, now: number): number => {
    samples.push([now, loaded])
    while (samples.length > 2 && now - samples[0][0] > windowMs) samples.shift()
    const [t0, l0] = samples[0]
    const dt = now - t0
    if (dt < 120) return smoothed // 样本太少先沿用上次
    const inst = ((loaded - l0) * 1000) / dt
    smoothed = smoothed > 0 ? smoothed * 0.35 + inst * 0.65 : inst
    return smoothed
  }
}

/** 一帧进度（统一在这里算百分比 / 速度 / 预计剩余） */
function emitProgress(
  onProgress: CloudProgressFn | undefined,
  phase: CloudProgress['phase'],
  loaded: number,
  total: number,
  elapsedMs: number,
  speedBps: number,
  extra: Partial<CloudProgress> = {},
): void {
  if (!onProgress) return
  const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : -1
  let etaMs: number | null = null
  if (total > 0 && speedBps > 0 && loaded < total) etaMs = ((total - loaded) / speedBps) * 1000
  onProgress({
    phase,
    loaded,
    total,
    percent,
    speedBps,
    elapsedMs,
    etaMs,
    ...extra,
  })
}

/** 用 XHR 上传（浏览器里唯一能拿到上传进度的通道） */
function sendUploadWithXhr(
  url: string,
  form: FormData,
  expectedBytes: number,
  onProgress?: CloudProgressFn,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const t0 = Date.now()
    const meter = makeSpeedMeter()

    xhr.open('POST', url, true)
    xhr.timeout = TIMEOUT_BULK_MS

    if (xhr.upload) {
      xhr.upload.onprogress = (e) => {
        const total = e.lengthComputable ? e.total : expectedBytes
        // 上传阶段**最多到 99%**：100% 留给"服务器确认收妥"。
        // 字节传完 ≠ 服务端处理完，这一点在旧版本上踩过坑（见 waitingServer 注释）。
        const capped = total > 0 ? Math.min(e.loaded, total - 1) : e.loaded
        emitProgress(onProgress, 'upload', capped, total, Date.now() - t0, meter(capped, Date.now()))
      }
      xhr.upload.onload = () => {
        // 字节已发完，等服务端响应：明确告知用户，别让进度条假装 100%
        emitProgress(onProgress, 'upload', expectedBytes, expectedBytes, Date.now() - t0, 0, {
          percent: 99,
          waitingServer: true,
        })
      }
      // 有些浏览器在 body 很小 / 被缓冲时不给 upload 事件，先发一个 0%
      emitProgress(onProgress, 'upload', 0, expectedBytes, 0, 0)
    }

    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText ?? '' })
    xhr.onerror = () => reject(new Error('NETWORK'))
    xhr.ontimeout = () => reject(new Error('TIMEOUT'))
    xhr.onabort = () => reject(new Error('ABORT'))
    // 注意：传 FormData 时**不能**自己设 Content-Type —— 浏览器要带 boundary
    xhr.send(form)
  })
}

/** 没有 XHR 时的退路（Node 自测 / 极老环境）：没有百分比，只能报"在传" */
async function sendUploadWithFetch(
  url: string,
  form: FormData,
  expectedBytes: number,
  onProgress?: CloudProgressFn,
): Promise<{ status: number; text: string }> {
  const t0 = Date.now()
  emitProgress(onProgress, 'upload', 0, expectedBytes, 0, 0)
  // fetch 拿不到上传进度：先告诉界面"在传"，等响应回来再由调用方收尾。
  // 这里直接进"等服务端"态是对的 —— 字节已经交给 fetch，它自己分块发。
  emitProgress(onProgress, 'upload', expectedBytes, expectedBytes, Date.now() - t0, 0, {
    percent: 99,
    waitingServer: true,
  })
  let response: Response
  try {
    response = await fetch(url, { method: 'POST', body: form })
  } catch {
    throw new Error('NETWORK')
  }
  return { status: response.status, text: await response.text() }
}

/** 上传表单（字段名必须是 `file`，后端按这个名字取包） */
function buildUploadForm(json: string): { form: FormData; bytes: number } {
  const blob = new Blob([json], { type: 'application/json' })
  const form = new FormData()
  // 第三个参数是文件名：后端会按扩展名判断 json / zip，并记在导入记录里
  form.append('file', blob, `baker-data-${Date.now()}.json`)
  return { form, bytes: blob.size }
}

/**
 * 把导出的 JSON 包上传到后端，拿一个取件码
 *
 * @param json       与本地「导出 JSON」完全相同的包（调用方用 exportToJson 生成）
 * @param onProgress 进度回调（浏览器里有真实字节/速度）
 */
export async function uploadCloudData(
  json: string,
  onProgress?: CloudProgressFn,
): Promise<CloudUploadResult> {
  if (!json) throw new Error('没有可上传的数据')
  const { form, bytes } = buildUploadForm(json)
  const url = v1Url('/uploads')

  let res: { status: number; text: string }
  try {
    res =
      typeof XMLHttpRequest === 'undefined'
        ? await sendUploadWithFetch(url, form, bytes, onProgress)
        : await sendUploadWithXhr(url, form, bytes, onProgress)
  } catch (err) {
    const code = err instanceof Error ? err.message : ''
    if (code === 'TIMEOUT') throw new Error('上传超时：包太大或网络太慢，请稍后重试')
    if (code === 'ABORT') throw new Error('上传已取消')
    throw new Error('上传失败：连不上服务器，请检查网络后重试')
  }

  // 后端契约是 201；兼容 200（反代/中间层把它改成 200 时不至于误判失败）
  if (res.status !== 200 && res.status !== 201) {
    throw new Error(cloudErrorText('upload', res.status, res.text))
  }
  let data: CloudUploadResult | null = null
  try {
    data = JSON.parse(res.text) as CloudUploadResult
  } catch {
    throw new Error('上传失败：服务器返回了看不懂的内容')
  }
  if (!data?.code) throw new Error('上传失败：服务器没有返回取件码')
  return data
}

// ---------------------------------------------------------------------------
// 验码 / 领取
// ---------------------------------------------------------------------------

/** 把 fetch 的 JSON 结果读成对象（读不出来就报一句人话） */
function parseJsonOrThrow<T>(text: string, what: string): T {
  try {
    return JSON.parse(text) as T
  } catch {
    throw new Error(`${what}：服务器返回了看不懂的内容`)
  }
}

/**
 * 验码（免登录）
 *
 * 为什么要单独验一次：领取是个"会写库"的动作，先验码能把 404（输错）
 * 与 410（过期）在第一时间说清楚，不用等领取请求失败。
 */
export async function verifyUploadCode(code: string): Promise<CloudUploadInfo> {
  const clean = normalizeClaimCode(code)
  if (!clean) throw new Error('请输入取件码')
  if (!isClaimCodeValid(clean)) throw new Error('取件码是 8 位字母或数字，请检查一下')

  let response: Response
  try {
    response = await fetch(v1Url(`/uploads/${encodeURIComponent(clean)}`), { method: 'GET' })
  } catch {
    throw new Error('连不上服务器：请检查网络后重试')
  }
  const text = await response.text()
  if (!response.ok) throw new Error(cloudErrorText('verify', response.status, text))
  const info = parseJsonOrThrow<CloudUploadInfo>(text, '验码失败')
  if (!info?.code) throw new Error('验码失败：服务器没有返回取件码信息')
  return info
}

/**
 * 把取件码里的包领取到当前设备 / 账号名下（**必须已登录**）
 *
 * `apiJson` 会自动带上设备 token，并在 401 时重试一次（token 失效自愈）。
 * 返回形状与 POST /v1/imports 相同：这是"导入了什么"的唯一凭据。
 */
export async function claimUploadCode(code: string): Promise<CloudClaimResult> {
  const clean = normalizeClaimCode(code)
  if (!clean) throw new Error('请输入取件码')
  if (!isClaimCodeValid(clean)) throw new Error('取件码是 8 位字母或数字，请检查一下')

  try {
    return await apiJson<CloudClaimResult>('/uploads/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: clean }),
    })
  } catch (err) {
    if (err instanceof ApiError) {
      // ApiError 里已经解析出 HTTP 状态 / retry_after，这里翻成人话再往上抛
      throw new Error(cloudErrorText('claim', err.status, '', err.message, err.retryAfter))
    }
    throw err
  }
}

/**
 * 先验码、再领取（界面上的两步）
 *
 * 合成一个函数是因为两个入口（数据管理页 / 账号页）流程完全一样，
 * 分开写两遍迟早会走散。验码成功但仍然 404/410 的情况由 claim 自己报错。
 */
export async function claimAfterVerify(code: string): Promise<CloudClaimResult> {
  const info = await verifyUploadCode(code)
  return claimUploadCode(info.code)
}

/**
 * 问一次"最近一次导入是什么"（GET /v1/imports，要登录）
 *
 * 用途：注册时带了取件码 —— 包是后端在 register 里领的，前端手里没有
 * claim 的返回体，只能从导入记录里把"刚领到什么"捞出来显示给用户。
 * 失败（网络/没记录）一律返回 null，不影响注册成功这件事。
 */
export async function fetchLatestImport(): Promise<CloudClaimResult | null> {
  try {
    const data = await apiJson<{ items?: CloudClaimResult[] }>('/imports', { method: 'GET' })
    return data.items?.[0] ?? null
  } catch {
    return null
  }
}

/**
 * 领取结果 → 一句话摘要（界面直接显示）
 *
 * 只报能看懂的四个数：会话 / 消息 / 图片 / 跳过；`duplicate` 单独说一句，
 * 否则用户会以为"我没领到东西"（其实是同一份包之前已经领过了）。
 *
 * @param prefix 自定义开头（注册时带码那条路要说"注册时已领取"，见 SettingsDialog）
 */
export function describeClaimResult(result: CloudClaimResult, prefix?: string): string {
  const parts = [
    `${result.conversations ?? 0} 段会话`,
    `${result.messages ?? 0} 条消息`,
  ]
  if (result.images > 0) parts.push(`${result.images} 张图片`)
  if (result.cards > 0) parts.push(`${result.cards} 个干员`)
  if (result.skipped > 0) parts.push(`跳过 ${result.skipped} 项`)
  const head = prefix
    ?? (result.duplicate
      ? '这份包这台设备之前领过，后端按内容去重（没有重复导入）：'
      : '已领取：')
  return `${head}${parts.join(' · ')}`
}
