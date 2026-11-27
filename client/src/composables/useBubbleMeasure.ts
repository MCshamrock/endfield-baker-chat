// =============================================================================
// useBubbleMeasure:气泡尺寸测量(带缓存 + 批量)
// -----------------------------------------------------------------------------
// computed 重算时同一段文本会被反复测量,用 Map 缓存避免重复触发 ruler DOM
// 重排。缓存键 = innerMax + metrics + text:移动端与桌面端最大内宽不同,
// 同一段文本在不同宽度下的换行结果不同,必须区分。
//
// 缓存必须有上限:键里含**全文**,而流式回复每个 token 都会产生一个新键
// (同一段回复的每个前缀各占一条),一次长回复就能堆出上千条、且整份会话
// 期间不释放。这里按 LRU 淘汰,容量见 CACHE_MAX。
//
// 2026-11-22(4.0.3)新增两条路径:
//   1. measure()      —— 单条精确测量,流式追加用(一次只量一段,开销可忽略)
//   2. request()      —— 布局管线用:命中缓存直接返回;**未命中先给 canvas 估算
//                        并记进待测量队列**,由 flush() 一次性批量实测
//     + flush()       —— 把队列里的文本交给 measureBubbleBatch(一次强制布局)
//
// 为什么要有第 2 条:9000 条消息的会话打开时,布局要把每条气泡都量一遍。
// 逐条测量 = 每条一次强制布局,实测 1745ms(低端安卓更久),这就是
// "点一下要七八秒才弹出来"的主因。批量后同一份数据 ~300ms。
//
// clearCache() 用于缓存整体失效(切换会话 / 清空消息 / 字号几何变化)。
// 调用点不在本文件:需要的地方从 useBubbleMeasure() 取 clearCache 调用即可
// (缓存是模块级的,任意一处调用对所有调用方生效)。
// =============================================================================

import {
  DESKTOP_BUBBLE_METRICS,
  estimateBubbleSize,
  measureBubble,
  measureBubbleBatch,
  type BatchMeasureItem,
  type BubbleBox,
  type BubbleMetrics,
} from '../utils/measure'

/**
 * 测量缓存容量上限(条)
 *
 * 9000 条消息的大存档要能一次全部装下,否则重新打开/重排时会反复实测,
 * 又回到"D 天前那个七八秒"。20000 条约等于两个 9000 条会话,键里含全文,
 * 内存量级是几 MB —— 相比一次 9000 条实测(几秒 CPU)完全值得。
 */
const CACHE_MAX = 20000

/** 测量缓存(innerMax|metrics|text -> BubbleBox;Map 迭代顺序 = 插入顺序) */
const cache = new Map<string, BubbleBox>()

/** 写入缓存并维持容量上限(最久未使用的先淘汰) */
function remember(key: string, value: BubbleBox): void {
  cache.set(key, value)
  if (cache.size <= CACHE_MAX) return
  // 一次淘汰一批(2%),避免每个新键都做一次 delete 维护
  const drop = Math.max(1, Math.floor(CACHE_MAX * 0.02))
  let n = 0
  for (const oldest of cache.keys()) {
    cache.delete(oldest)
    if (++n >= drop) break
  }
}

/** metrics 序列化(缓存键用;对象字段顺序固定) */
function metricsKey(m: BubbleMetrics): string {
  return `${m.fontSize}|${m.lineHeight}|${m.padX}|${m.padY}|${m.minW}|${m.minH}`
}

/** 缓存键(单条 / 批量 / 排队共用同一把) */
function cacheKey(text: string, innerMax: number, metrics: BubbleMetrics): string {
  return `${innerMax}|${metricsKey(metrics)}|${text}`
}

/**
 * 待批量实测的文本队列
 *
 * 用模块级:布局管线可能在同一次渲染里从多个组件调用 request(),
 * 队列必须是同一份,flush() 才能一次量完。
 */
const pending: BatchMeasureItem[] = []

/**
 * 气泡测量 composable
 */
export function useBubbleMeasure() {
  /**
   * 单条精确测量(命中缓存则直接返回)
   *
   * 流式追加 / 导出 / 临时量一段文本都走这里 —— 一次就量一条,
   * 直接走 DOM 实测,不等批量。
   */
  function measure(
    text: string,
    innerMax?: number,
    metrics: BubbleMetrics = DESKTOP_BUBBLE_METRICS,
  ): BubbleBox {
    const max = innerMax ?? 634
    const key = cacheKey(text, max, metrics)
    const hit = cache.get(key)
    if (hit) {
      // 命中即刷新为"最近使用":先删再插,移到 Map 队尾。
      // 不做这步就退化成 FIFO,热点文本(反复重渲染的长消息)会被新键挤掉。
      cache.delete(key)
      cache.set(key, hit)
      return hit
    }
    const result = measureBubble(text, max, metrics)
    remember(key, result)
    return result
  }

  /**
   * 布局管线专用:命中缓存就返回;未命中先返回 canvas 估算,并登记待批量测量
   *
   * 调用方在整轮布局结束后调用 flush() —— 只要有命中失败,就重新布局一遍
   * (第二遍全部命中缓存,拿到精确尺寸)。这样即使一次要量 9000 条,
   * 也只需要一次强制布局,而不是 9000 次。
   */
  function request(
    text: string,
    innerMax?: number,
    metrics: BubbleMetrics = DESKTOP_BUBBLE_METRICS,
  ): BubbleBox {
    const max = innerMax ?? 634
    const key = cacheKey(text, max, metrics)
    const hit = cache.get(key)
    if (hit) {
      cache.delete(key)
      cache.set(key, hit)
      return hit
    }
    pending.push({ text, innerMax: max, metrics })
    // 占位值(不写缓存):精确值由 flush() 批量实测后写入
    return estimateBubbleSize(text, max, metrics)
  }

  /**
   * 批量实测队列里的文本
   *
   * @returns count = 本次真正实测了几条（0 = 队列空，无需重新布局）；
   *          texts = 这批文本原文（布局层用它作废"用估算排过的那几条"的行缓存）
   *
   * 为什么要回传 texts：布局层按消息缓存了整行（`msgCache`），那些行是用
   * canvas 估算排的。只清行缓存而不清消息缓存的话，第二遍仍会复用到估算值，
   * 精确测量永远落不了地 —— 多段文本就会一直按"少一行"的高度渲染并被裁掉。
   */
  function flush(): { count: number; texts: Set<string> } {
    if (pending.length === 0) return { count: 0, texts: new Set() }
    // 去重:同一段文本可能在一次布局里被请求多次(分条/居中行/相邻消息)
    const uniq = new Map<string, BatchMeasureItem>()
    for (const item of pending) uniq.set(cacheKey(item.text, item.innerMax, item.metrics), item)
    pending.length = 0
    const items = [...uniq.values()]
    const boxes = measureBubbleBatch(items)
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      remember(cacheKey(item.text, item.innerMax, item.metrics), boxes[i])
    }
    return { count: items.length, texts: new Set(items.map((it) => it.text)) }
  }

  /** 清空缓存(切换会话 / 清空消息后调用,避免继续命中旧内容的测量结果) */
  function clearCache() {
    cache.clear()
    pending.length = 0
  }

  return { measure, request, flush, clearCache }
}
