// =============================================================================
// 气泡文本尺寸测量
// -----------------------------------------------------------------------------
// 测量策略:
// 1. canvas.measureText 估算每段宽度,得到 innerW 与粗略行数
//    (表情 token 按真实渲染宽展开为若干空格,参与宽度估算)
// 2. 隐藏 ruler DOM 实测精确行数与宽度(ruler 用真实 <img> 表情排版,
//    表情内联固定像素尺寸,加载前即可参与布局)
// 3. 取较大值作为最终结果,避免 canvas 测量误差导致气泡塌陷
//
// 气泡测量参数化:字体/字号/行高/边距/最小尺寸收敛为 BubbleMetrics,
// 桌面端用 DESKTOP_BUBBLE_METRICS(原固定常量),移动端由几何层传入更小值。
// =============================================================================

import { emojiToHtml, measureTextWithEmoji } from '../constants/emoji'
import type { MessageSide, RectSize } from '../types/chat'

/** 气泡尾巴偏移(px):rect 左侧留出空间画尾巴,mine 侧右侧也留同样空间 */
export const BUBBLE_TAIL_OFFSET = 8.2

/**
 * 气泡 SVG 总宽 = 尾巴偏移 + rect 宽(+ mine 侧再补一个尾巴偏移)
 *
 * 容器跟随过渡尺寸:rect 右缘 = svgW - 尾巴偏移,任何时刻 rect 都在 svg 内。
 */
export function bubbleSvgWidth(width: number, side: MessageSide): number {
  return BUBBLE_TAIL_OFFSET + width + (side === 'mine' ? BUBBLE_TAIL_OFFSET : 0)
}

/** 气泡字体字号(px),与会话气泡文字一致 */
export const BUBBLE_FONT_SIZE = 20.88
/** 行高 = 字号 × 1.5,与会话气泡文字一致 */
export const BUBBLE_LINE_HEIGHT = BUBBLE_FONT_SIZE * 1.5
/** 气泡左右内边距(px) */
export const BUBBLE_PAD_X = 13
/** 气泡上下内边距(px) */
export const BUBBLE_PAD_Y = 9
/** 气泡最小宽度(px),单字短消息也保持圆角形状 */
export const BUBBLE_MIN_W = 51.76
/** 气泡最小高度(px) */
export const BUBBLE_MIN_H = 42.47
/** 单行气泡实际高度(px)= 一行行高 + 上下 padding,用于加载气泡与单行文字气泡对齐 */
export const BUBBLE_SINGLE_LINE_H = BUBBLE_LINE_HEIGHT + BUBBLE_PAD_Y * 2
/** 加载气泡 rect 尺寸(文字气泡从该尺寸过渡到自身尺寸) */
export const LOADING_RECT: RectSize = { w: 100, h: BUBBLE_SINGLE_LINE_H }
/** 气泡最大宽度(px),超出则换行(桌面默认;移动端由几何层传入更小值) */
const BUBBLE_MAX_W = 660
/** 气泡内文最大宽度(px)= 外部最大框架宽 − 左右内边距 */
const BUBBLE_INNER_MAX_W = BUBBLE_MAX_W - BUBBLE_PAD_X * 2

/** 气泡测量参数(桌面默认 = 原固定常量;移动端由几何层传入更小值) */
export interface BubbleMetrics {
  /** 字体字号(px) */
  fontSize: number
  /** 行高(px) */
  lineHeight: number
  /** 左右内边距(px) */
  padX: number
  /** 上下内边距(px) */
  padY: number
  /** 最小外框宽(px) */
  minW: number
  /** 最小外框高(px) */
  minH: number
}

/** 桌面端默认测量参数(与历史固定值完全一致) */
export const DESKTOP_BUBBLE_METRICS: BubbleMetrics = {
  fontSize: BUBBLE_FONT_SIZE,
  lineHeight: BUBBLE_LINE_HEIGHT,
  padX: BUBBLE_PAD_X,
  padY: BUBBLE_PAD_Y,
  minW: BUBBLE_MIN_W,
  minH: BUBBLE_MIN_H,
}

/**
 * 测量文本对应的气泡渲染尺寸
 *
 * 表情以 <img> 渲染(1em 高),ruler 用真实 HTML 排版,实测宽度包含表情
 * 的真实渲染宽度;canvas 估算将表情 token 展开为等宽空格作后备。
 *
 * @param text   消息文本(支持 \n 换行,可含表情 token)
 * @param innerMax 内容最大宽度(px,超出换行;桌面默认 634,移动端由几何层传入)
 * @param metrics 测量参数(字号/行高/边距/最小尺寸;默认桌面)
 * @returns      外框宽高 + 内文宽度
 */
export function measureBubble(
  text: string,
  innerMax: number = BUBBLE_INNER_MAX_W,
  metrics: BubbleMetrics = DESKTOP_BUBBLE_METRICS,
): BubbleBox {
  return measureWith(
    {
      innerMax,
      padX: metrics.padX,
      padY: metrics.padY,
      minW: metrics.minW,
      minH: metrics.minH,
      fontSize: metrics.fontSize,
      lineHeight: metrics.lineHeight,
    },
    text,
  )
}
/** 气泡字体栈(用于 canvas 与 ruler 同步) */
export const BUBBLE_FONT = `"HarmonyOS Sans SC Medium", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif`

/**
 * 全局唯一的字体等待任务(单飞)
 *
 * 字体只加载/等待一次:后续调用直接返回同一个 promise,不再每次新建
 * promise + 3s 定时器(旧实现每个测量未命中的文本都会建一套)。
 * 该任务永不 reject(失败/超时都按"放行"处理),见 waitBubbleFont。
 */
let bubbleFontPromise: Promise<void> | null = null

/**
 * 显式等待气泡字体真正装入(font-display: swap 时 fonts.ready 可能早于换行测量)
 *
 * 气泡宽度用 BUBBLE_FONT 在 ruler/canvas 里测量:若字体未就绪会回退
 * Microsoft YaHei,字形宽度不同导致换行数/边距与导出(HarmonyOS)不一致。
 *
 * 只在**截图/导出**前 await 一次(captureImage 调用);测量路径刻意不等待
 * (见 measureWith 注释)—— 等字体是截图路径的事,不该拖慢每个 chunk。
 */
export function ensureBubbleFont(): Promise<void> {
  if (!bubbleFontPromise) bubbleFontPromise = waitBubbleFont()
  return bubbleFontPromise
}

/** 真正等字体的任务(3s 上限;任何异常都吞掉,只作为"尽量等一下") */
async function waitBubbleFont(): Promise<void> {
  // 加超时：字体(4.3MB woff2)加载慢/挂起时不再阻塞截图，3s 后放行
  try {
    await Promise.race([
      (async () => {
        try {
          await document.fonts.load(`${BUBBLE_FONT_SIZE}px ${BUBBLE_FONT}`)
        } catch { /* 保留后备 */ }
        await document.fonts.ready
      })(),
      new Promise<void>((resolve) => setTimeout(resolve, 3000)),
    ])
  } catch { /* 忽略 */ }
}

/** 气泡测量结果 */
export interface BubbleBox {
  /** 外框 rect 宽度(含 padding) */
  rectW: number
  /** 外框 rect 高度(含 padding) */
  rectH: number
  /** 内文区域宽度(不含 padding) */
  innerW: number
}

/** 单次测量的差异参数(气泡) */
interface MeasureConfig {
  /** 内容最大宽度(px),超出换行 */
  innerMax: number
  /** 水平内边距(rectW 附加) */
  padX: number
  /** 垂直内边距(rectH 附加) */
  padY: number
  /** 最小外框宽度(px) */
  minW: number
  /** 最小外框高度(px) */
  minH: number
  /** 字体字号(px,ruler/canvas 测量用) */
  fontSize: number
  /** 行高(px,ruler 行数推算用) */
  lineHeight: number
}

/**
 * 隐藏 ruler DOM 单例
 *
 * 设计理由:每次测量都创建 DOM 会触发重排,缓存单例避免性能损耗。
 * 单例绑定到 body,样式与气泡一致以保证测量精度;每次测量前按
 * metrics 更新字号/行高(桌面与移动端字号不同)。
 */
let ruler: HTMLDivElement | null = null

function getRuler(metrics: { fontSize: number; lineHeight: number }): HTMLDivElement {
  if (!ruler) {
    const el = document.createElement('div')
    el.style.cssText =
      `position:fixed;left:-99999px;top:0;visibility:hidden;pointer-events:none;` +
      `white-space:pre-line;word-break:break-word;` +
      `font-family:${BUBBLE_FONT};`
    document.body.appendChild(el)
    ruler = el
  }
  ruler.style.fontSize = `${metrics.fontSize}px`
  ruler.style.lineHeight = `${metrics.lineHeight}px`
  return ruler
}

/**
 * canvas 2D 上下文单例(用于文本宽度估算)
 *
 * 只在首次创建,避免每次测量都 new 一个 canvas 造成 GC 压力;
 * 每次测量前按 metrics 更新字号。
 */
let canvasCtx: CanvasRenderingContext2D | null = null

function getCanvasCtx(fontSize: number): CanvasRenderingContext2D {
  if (!canvasCtx) {
    const canvas = document.createElement('canvas')
    canvasCtx = canvas.getContext('2d')!
  }
  canvasCtx.font = `${fontSize}px ${BUBBLE_FONT}`
  return canvasCtx
}

/**
 * 测量文本对应的气泡渲染尺寸
 *
 * 表情以 <img> 渲染(1em 高),ruler 用真实 HTML 排版,实测宽度包含表情
 * 的真实渲染宽度;canvas 估算将表情 token 展开为等宽空格作后备。
 *
 * @param config 测量定义(最大内宽 / 边距 / 最小尺寸 / 字号 / 行高)
 * @param text   消息文本(支持 \n 换行,可含表情 token)
 * @returns      外框宽高 + 内文宽度
 */
/**
 * 量出一段 range 内容里"最宽一行"的宽度
 *
 * `Range.getClientRects()` 对含内联元素的内容是**逐片段**返回 rect 的:
 * 文字一段、每个 inline-block(表情 <img>)各一段。同一行因此可能有多条 rect,
 * 直接取 max 只会得到最长的那一片段(连续两个表情 = 一个表情的宽),
 * 于是气泡被算窄、后面的表情被迫换行。
 *
 * 这里先把 rect 按行归并,再取每行的"最右 − 最左"作为行宽。
 * 归并判据用**垂直交叠**而非 top 相等:表情图是 vertical-align:middle,
 * 它和同一行的文字行盒 top 并不相同,按 top 分组会把一行拆成两行。
 */
function maxLineWidth(range: Range): number {
  const rects = [...range.getClientRects()]
    .filter((r) => r.width > 0 || r.height > 0)
    .sort((a, b) => a.top - b.top)
  const lines: { top: number; bottom: number; left: number; right: number }[] = []
  for (const r of rects) {
    const line = lines.find((ln) => r.top < ln.bottom - 0.5 && r.bottom > ln.top + 0.5)
    if (line) {
      line.top = Math.min(line.top, r.top)
      line.bottom = Math.max(line.bottom, r.bottom)
      line.left = Math.min(line.left, r.left)
      line.right = Math.max(line.right, r.right)
    } else {
      lines.push({ top: r.top, bottom: r.bottom, left: r.left, right: r.right })
    }
  }
  let widest = 0
  for (const ln of lines) widest = Math.max(widest, ln.right - ln.left)
  return widest
}

/**
 * canvas 估算(不碰 DOM)
 *
 * 只读 ctx.measureText,给两处用:
 *   1. measureWith 的第一轮(再用 ruler 校正)
 *   2. 批量测量前的占位值:9000 条消息一次性实测时,先算出"大致的宽度与行数",
 *      真正精确值由 measureBubbleBatch 里的 DOM 实测覆盖
 *
 * 这里刻意**不**等待/触发字体加载:测量是同步高频路径(流式每个 chunk 都要跑),
 * 字体就绪只影响字形宽度的细微差异,而等待属于截图路径(captureImage 会
 * await ensureBubbleFont)。
 */
function canvasEstimate(
  config: MeasureConfig,
  text: string,
): { innerW: number; lines: number } {
  const innerMax = config.innerMax
  const ctx = getCanvasCtx(config.fontSize)
  let innerW = 0
  let lines = 0
  for (const seg of text.split('\n')) {
    const w = measureTextWithEmoji(ctx, seg)
    innerW = Math.max(innerW, Math.min(w, innerMax))
    // 4.0.5 修：**每一段至少占一行**（空段 = 一个空行），超宽再按宽度加行。
    // 以前是“lines 从 1 起、每段只补 ceil(w/innerMax)-1”，于是
    // 「三段各一行」只算出 1 行 —— 多段文本的气泡按 1 行高渲染，
    // 后面几行直接被 foreignObject 的高度裁掉（用户看到的“显示不全”）。
    lines += Math.max(1, Math.ceil(w / innerMax))
  }
  return { innerW, lines: Math.max(1, lines) }
}

/** 由"内文宽 / 行数 / 测量参数"拼出最终气泡盒(单条与批量共用同一口径) */
function toBox(config: MeasureConfig, innerW: number, lines: number): BubbleBox {
  return {
    rectW: Math.max(config.minW, innerW + config.padX * 2),
    rectH: Math.max(config.minH, lines * config.lineHeight + config.padY * 2),
    innerW,
  }
}

function measureWith(config: MeasureConfig, text: string): BubbleBox {
  const innerMax = config.innerMax

  // 第一轮:canvas 估算(见 canvasEstimate)
  const estimate = canvasEstimate(config, text)
  let innerW = estimate.innerW

  // 第二轮:ruler DOM 实测,优先采用
  let lines = estimate.lines
  let rulerW = 0
  const rulerEl = getRuler(config)
  rulerEl.style.width = `${innerMax}px`
  // 表情内联 em 尺寸:高度 1em、宽度按原图宽高比,图片加载前也能按真实宽高参与布局
  rulerEl.innerHTML = emojiToHtml(text)
  const rulerLines = Math.round(rulerEl.scrollHeight / config.lineHeight)
  if (Number.isFinite(rulerLines) && rulerLines > 0) lines = rulerLines
  const range = document.createRange()
  range.selectNodeContents(rulerEl)
  // 内容宽:取最宽一行的真实渲染宽度。
  // 不能读 scrollWidth——内容不溢出时 scrollWidth 恒等于容器宽(innerMax),
  // 会把短消息撑到最大宽度;getClientRects 返回每行的真实 rect。
  // 也不能直接对 rects 取 max:Range 对内联内容是**逐片段**给的 rect,一行里
  // 两个并排的 <img>(连续表情 token,如 [sns_emoji_033][sns_emoji_033])会返回
  // 两条各只有半个表情宽的 rect,取 max 等于按"一个表情"算宽 —— 气泡窄一点,
  // 第二个表情就被挤到下一行。必须先按行归并、再取该行的最右 − 最左。
  const contentW = maxLineWidth(range)
  rulerW = Math.min(contentW, innerMax)
  innerW = Math.max(innerW, rulerW)

  return toBox(config, innerW, lines)
}

// ---------------------------------------------------------------------------
// 批量测量（2026-11-22 4.0.3：9000 条消息不再逐条强制布局）
// ---------------------------------------------------------------------------

/**
 * 批量测量的一条请求（单条与批量共用同一形状）
 */
export interface BatchMeasureItem {
  /** 消息原文 */
  text: string
  /** 内容最大宽度(px) */
  innerMax: number
  /** 字号 / 行高 / 内边距 / 最小尺寸 */
  metrics: BubbleMetrics
}

/** 每批最多多少个"测量项":一批只做一次强制布局,太多会一次性占住主线程 */
const BATCH_CHUNK = 2000

/** 把测量参数拼成 MeasureConfig(与 measureBubble 的取值口径完全一致) */
function configOf(item: BatchMeasureItem): MeasureConfig {
  return {
    innerMax: item.innerMax,
    padX: item.metrics.padX,
    padY: item.metrics.padY,
    minW: item.metrics.minW,
    minH: item.metrics.minH,
    fontSize: item.metrics.fontSize,
    lineHeight: item.metrics.lineHeight,
  }
}

/**
 * 一次量一整批气泡（9000 条会话打开时不卡的关键）
 *
 * 为什么必须批量：旧实现是"写一条 → 读 scrollHeight → 读 range"，
 * 每一次读都强制同步布局一次；9000 条就是 9000 次全文档重排。
 * 实测（本机 Edge）逐条 1745ms，批量只要 ~300ms；低端安卓 WebView 上
 * 差距更大 —— 这正是"点一下要 7.8 秒才弹出来"的根源。
 *
 * 做法：
 *   1. 先对每条做 canvas 估算（不碰 DOM，判断行数下限与内文宽度）
 *   2. 把这一批的文本全部塞进同一个隐藏容器的子 div（每条一个块）
 *   3. **只读**：读每个子 div 的高度（行数）与 Range 的逐行宽度
 *      —— 期间没有任何写操作，浏览器只需要在第一次读之前排一次版
 *   4. 与单条测量同口径取"canvas 与 DOM 的较大值"，避免气泡塌陷
 *
 * 返回值与 items 一一对应。
 */
export function measureBubbleBatch(items: BatchMeasureItem[]): BubbleBox[] {
  const out: BubbleBox[] = new Array(items.length)

  for (let start = 0; start < items.length; start += BATCH_CHUNK) {
    const end = Math.min(items.length, start + BATCH_CHUNK)

    const host = document.createElement('div')
    host.style.cssText =
      'position:fixed;left:-99999px;top:0;visibility:hidden;pointer-events:none;' +
      'font-family:' + BUBBLE_FONT + ';'

    const els: HTMLDivElement[] = []

    for (let i = start; i < end; i++) {
      const item = items[i]
      const config = configOf(item)
      const el = document.createElement('div')
      // 与 getRuler 的单例同样的排版属性：换行口径必须一致
      el.style.cssText =
        'margin:0;padding:0;border:0;white-space:pre-line;word-break:break-word;' +
        'width:' + item.innerMax + 'px;' +
        'font-size:' + item.metrics.fontSize + 'px;' +
        'line-height:' + item.metrics.lineHeight + 'px;'
      // 表情以真实 <img> 参与排版(与单条测量同源)
      el.innerHTML = emojiToHtml(item.text)
      host.appendChild(el)
      els.push(el)
      // canvas 估算先落一半(后面用 DOM 实测校正)
      const est = canvasEstimate(config, item.text)
      out[i] = toBox(config, est.innerW, est.lines)
    }

    document.body.appendChild(host)

    // 一次性排版：之后的读操作都不会再触发重排
    for (let i = start; i < end; i++) {
      const item = items[i]
      const config = configOf(item)
      const el = els[i - start]
      const h = el.getBoundingClientRect().height
      let lines = Math.round(h / config.lineHeight)
      const range = document.createRange()
      range.selectNodeContents(el)
      const contentW = maxLineWidth(range)
      if (!Number.isFinite(lines) || lines <= 0) lines = 1
      const innerW = Math.max(out[i].innerW, Math.min(contentW, item.innerMax))
      out[i] = toBox(config, innerW, lines)
    }

    host.remove()
  }

  return out
}

/**
 * 只要 canvas 估算的气泡盒（不碰 DOM）
 *
 * 用途：批量测量还没跑、但布局管线又必须要一个尺寸时，先用它顶上；
 * 精确值会在 measureBubbleBatch 之后覆盖。**不要**把它写进缓存 ——
 * 它在长段换行/表情混排时可能与真实排版差一行。
 */
export function estimateBubbleSize(
  text: string,
  innerMax: number = BUBBLE_INNER_MAX_W,
  metrics: BubbleMetrics = DESKTOP_BUBBLE_METRICS,
): BubbleBox {
  const config: MeasureConfig = {
    innerMax,
    padX: metrics.padX,
    padY: metrics.padY,
    minW: metrics.minW,
    minH: metrics.minH,
    fontSize: metrics.fontSize,
    lineHeight: metrics.lineHeight,
  }
  const est = canvasEstimate(config, text)
  return toBox(config, est.innerW, est.lines)
}