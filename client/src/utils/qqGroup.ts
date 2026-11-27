// =============================================================================
// qqGroup —— 关于面板「加入QQ群」的跳转
// -----------------------------------------------------------------------------
// 网页里那张群二维码下面跟的是 qun.qq.com 的 H5 分享页。手机浏览器里点它会
// 再弹一次「打开QQ」;而在应用的 WebView 里点,只会把 H5 套在应用自己身上,
// 用户还得手动再点一下「打开QQ」—— 真机反馈就是"点了没反应"。
//
// 所以安卓端走本地插件 QqGroup(直接 mqqapi 唤起 QQ 群名片),没装 QQ 才退回 H5;
// 浏览器 / EXE / 网页版保持原行为(开新页,Windows 交给默认浏览器)。
//
// 注意:plugin 只有在真机上才注册;网页里 registerPlugin 返回的是"万能假函数",
// 直接调用会抛 "not implemented on web",因此**必须先判平台**(见 utils/platform.ts)。
// =============================================================================
import { registerPlugin } from '@capacitor/core'
import { isAndroid } from './platform'

/** 原生插件的最小接口(与 MainActivity 里注册的 @CapacitorPlugin(name="QqGroup") 对应) */
interface QqGroupPluginLike {
  open?: (opts: { uin: string; url?: string }) => Promise<{ ok?: boolean; via?: string }>
}

const QqGroup = registerPlugin<QqGroupPluginLike>('QqGroup')

/** 跳转超时:真机上 startActivity 是同步的,这里只兜住"插件没应答"的极端情况 */
const OPEN_TIMEOUT_MS = 6_000

/** 浏览器 / Electron:开新页 */
function openInBrowser(url: string): void {
  try {
    window.open(url, '_blank', 'noopener')
  } catch {
    // 弹窗被拦截时退回当前页跳转,宁可离开也不让点击没反应
    window.location.href = url
  }
}

/**
 * 打开 QQ 群
 *
 * @returns 是否已交给原生/浏览器处理(界面没有反馈需求,调用方可以忽略)
 */
export async function openQqGroup(group: { code: string; url: string }): Promise<boolean> {
  if (isAndroid()) {
    try {
      const res = await Promise.race([
        QqGroup.open ? QqGroup.open({ uin: group.code, url: group.url }) : Promise.resolve(null),
        new Promise<null>((resolve) => window.setTimeout(() => resolve(null), OPEN_TIMEOUT_MS)),
      ])
      if (res && res.ok) return true
      // 原生没成功(超时 / 两种都打不开)→ 退回浏览器开新页,别让点击变成没反应
      openInBrowser(group.url)
      return false
    } catch {
      openInBrowser(group.url)
      return false
    }
  }

  openInBrowser(group.url)
  return true
}
