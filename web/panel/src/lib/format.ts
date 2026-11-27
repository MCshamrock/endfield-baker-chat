/**
 * format.ts —— 所有"数字和时间怎么显示"的函数集中在这里。
 *
 * 为什么要集中：后台一屏里几十个数字和日期，散着写就会出现
 * "这一列 38630、那一列 38,630""这里 11-15、那里 2026.11.15"，
 * 看着像两个人做的。集中之后还有一个好处：改口径只改一处。
 *
 * 两条口径写死在这里：
 * 1. **数字用等宽数字、千分位逗号**（对齐全靠字体，不靠空格硬凑）；
 * 2. **时间一律显示本地时区**。后端给的是 ISO UTC（`2026-11-17T17:32:40+00:00`），
 *    `new Date()` 会转成本机时区 —— 面板上看到的永远是运维自己表上的时间。
 *    唯一的例外是"按天聚合"的日期：那是后端按**东八区（UTC+8）**切好的日子，
 *    只是字符串，不能再转，转了就会跨日错位。这类日期用 `fmtDay`，补零用 `lastStatsDays`。
 */

const EMPTY = '—'

/** 千分位整数。null / undefined / NaN 一律给破折号，不要显示 "NaN"。 */
export function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY
  return Math.round(value)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** token 数：上万用"万"，这样 158,522 显示成 15.9 万，一列里宽度差不多 */
export function fmtToken(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(1)} 万`
  return fmtInt(value)
}

/** 毫秒：小于 1 秒按毫秒说，超过 1 秒按秒说（2916 → 2.9 s） */
export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY
  if (value <= 0) return '0 ms'
  if (value < 1000) return `${Math.round(value)} ms`
  return `${(value / 1000).toFixed(1)} s`
}

/** 比例：`fmtPct(25, 47)` → "53%"；分母是 0 时给破折号（不是 0%） */
export function fmtPct(part: number, total: number, digits = 0): string {
  if (!total || !Number.isFinite(part) || !Number.isFinite(total)) return EMPTY
  return `${((part / total) * 100).toFixed(digits)}%`
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/** 把 ISO 串解析成 Date；解析不了给 null（后端偶尔给空串） */
function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

/** 本地时区的完整时间：`2026.11.18 01:32` */
export function fmtDateTime(iso: string | null | undefined): string {
  const d = toDate(iso)
  if (!d) return EMPTY
  return `${fmtDateObj(d)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}

/** 本地时区的日期：`2026.11.18` */
export function fmtDate(iso: string | null | undefined): string {
  const d = toDate(iso)
  return d ? fmtDateObj(d) : EMPTY
}

function fmtDateObj(d: Date): string {
  return `${d.getFullYear()}.${pad2(d.getMonth() + 1)}.${pad2(d.getDate())}`
}

/** 本地时区的时分秒：`01:32:40`（思考链那种要精确到秒的地方） */
export function fmtTime(iso: string | null | undefined): string {
  const d = toDate(iso)
  if (!d) return EMPTY
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}

/**
 * 按天聚合的日期（`YYYY-MM-DD`，**东八区日**：后端把 UTC 时间 +8 小时再取日期）。
 * **只做字符串替换，不经过 Date** —— 一旦 `new Date('2026-11-15')`，
 * 浏览器会当成 UTC 零点，在东八区之外的地方就会变成前一天。
 */
export function fmtDay(day: string | null | undefined): string {
  if (!day) return EMPTY
  // 用 split/join 而不是 replaceAll：构建目标是 es2019，replaceAll 是 es2021 才有的
  return day.split('-').join('.')
}

/** 图表横轴用的短日期：`11.15` */
export function fmtDayShort(day: string | null | undefined): string {
  if (!day) return EMPTY
  const parts = day.split('-')
  return parts.length === 3 ? `${parts[1]}.${parts[2]}` : day
}

/** 站点日界偏移（小时）。后端按东八区切天，前端补零必须用同一个偏移，否则最后一根柱子会错位 */
const STATS_TZ_OFFSET_HOURS = 8
const STATS_TZ_OFFSET_MS = STATS_TZ_OFFSET_HOURS * 3_600_000

/** 站点日键：和后端 `date(created_at + INTERVAL 8 HOUR)` 同一个口径，用于补零对齐 */
export function statsDayKey(d: Date): string {
  // 先按偏移推一下，再取 ISO 的日期部分：CST 没有夏令时，这样推是精确的
  return new Date(d.getTime() + STATS_TZ_OFFSET_MS).toISOString().slice(0, 10)
}

/**
 * 从"服务器现在的时刻"往前数 n 天，返回升序的站点日键（东八区）。
 * 后端只返回**有请求的那几天**，直方图必须自己补零，否则 7 根柱子会缩成 2 根。
 */
export function lastStatsDays(nowIso: string | null | undefined, n: number): string[] {
  const now = toDate(nowIso) ?? new Date()
  const out: string[] = []
  for (let i = n - 1; i >= 0; i -= 1) {
    out.push(statsDayKey(new Date(now.getTime() - i * 86_400_000)))
  }
  return out
}

/** 本机时区标签，写在页头让"看到的时间是哪儿的"不含糊：`UTC+8` */
export function localZoneLabel(): string {
  try {
    const offset = -new Date().getTimezoneOffset() // 分钟，东正西负
    const sign = offset >= 0 ? '+' : '-'
    const abs = Math.abs(offset)
    const h = Math.floor(abs / 60)
    const m = abs % 60
    return `UTC${sign}${h}${m ? `:${pad2(m)}` : ''}`
  } catch {
    return '本地时区'
  }
}

/** 文本截断（表格里长显示名/长键名）。按字符数，不按字节。 */
export function clip(text: string | null | undefined, max: number): string {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}…` : text
}
