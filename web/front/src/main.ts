import { isElectron, isNative } from './utils/platform'

/**
 * 按平台注册品牌字体（@font-face）
 *
 * - Electron（EXE）/ Capacitor（APK）：字体文件在包内，本地 IO，几乎零成本，
 *   保留 HarmonyOS Sans 的品牌观感。
 * - 网页版：不注册。全量中文字体 4.3MB，实测在测试版链路上单这一个文件就要
 *   ~29 秒（149 KB/s），是首屏最大的单项开销；不注册时文字自动回退到
 *   MiSans / PingFang SC / 微软雅黑 —— 观感几乎无差别，首屏却快一个数量级。
 */
function registerBrandFont(): void {
  if (!isElectron() && !isNative()) return
  if (document.getElementById('brand-font')) return
  const style = document.createElement('style')
  style.id = 'brand-font'
  style.textContent =
    "@font-face{font-family:'HarmonyOS Sans SC Medium';" +
    "src:url('/fonts/HarmonyOS_Sans_SC_Medium.woff2') format('woff2');" +
    'font-weight:500;font-style:normal;font-display:swap}'
  document.head.appendChild(style)
}

registerBrandFont()

import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { useChatStore } from './stores/chat'
import { useSettingsStore } from './stores/settings'
import { hydrateLegacyCards } from './utils/legacyLocalView'
import { useChatPersistence } from './composables/useChatPersistence'
import { useBubbleMeasure } from './composables/useBubbleMeasure'
import { BUBBLE_FONT_SIZE, BUBBLE_FONT } from './utils/measure'
import './styles/main.scss'

/**
 * 启动失败兜底页
 *
 * 启动链路(读本地库 / 建应用 / 首次挂载)任何一步抛错, #app 里就只剩
 * 静态骨架或半截 DOM —— 用户看到的是"永远加载中",和卡死无法区分。
 * 这里用原生 DOM 画一页可见的错误说明;样式内联在 index.html 里,
 * 所以即使 Vue 完全没起来、构建 CSS 没加载,也能正常显示。
 */
function renderBootError(error: unknown): void {
  const host = document.getElementById('app')
  if (!host) return

  const detail = error instanceof Error ? error.message : String(error)

  const wrap = document.createElement('div')
  wrap.className = 'boot-error'
  wrap.setAttribute('role', 'alert')

  const title = document.createElement('h1')
  title.className = 'boot-error__title'
  title.textContent = '启动失败'

  const hint = document.createElement('p')
  hint.className = 'boot-error__hint'
  hint.textContent = '本地数据不会被删除。请先重新加载；若仍然失败，把下面的信息反馈给开发者。'

  const pre = document.createElement('pre')
  pre.className = 'boot-error__detail'
  pre.textContent = detail || '未知错误'

  const retry = document.createElement('button')
  retry.className = 'boot-error__retry'
  retry.type = 'button'
  retry.textContent = '重新加载'
  retry.addEventListener('click', () => location.reload())

  wrap.append(title, hint, pre, retry)
  host.replaceChildren(wrap)
}

/**
 * 气泡字体后台预热
 *
 * 全量中文字体有 4.3MB。若像以前那样把 fonts.load / fonts.ready 放进挂载
 * 门槛,弱网下首屏会一直停在骨架;而字体本身是 font-display: swap,
 * 先用系统字体显示完全可用。因此改成挂载后再预热,失败也只是回退字体。
 *
 * 字体就绪后字形宽度才最终确定:之前按回退字体量出的气泡尺寸必须作废,
 * 并强制一次重排,避免气泡宽度/换行停在回退字体上。
 */
function prewarmBubbleFont(): void {
  const fonts = typeof document !== 'undefined' ? document.fonts : null
  if (!fonts) return

  void (async () => {
    try {
      await fonts.load(`${BUBBLE_FONT_SIZE}px ${BUBBLE_FONT}`)
      await fonts.ready
    } catch {
      // 字体加载失败:沿用系统回退字体,不影响任何功能
      return
    }

    // 测量缓存是模块级单例,这里取用不需要组件上下文
    useBubbleMeasure().clearCache()
    requestAnimationFrame(() => {
      // 读一次布局属性,强制浏览器在字体替换后完成同步重排
      void document.body.offsetHeight
      // 几何 / 缩放模块监听 resize:借这一拍让它们按最终字体重新量一遍
      window.dispatchEvent(new Event('resize'))
    })
  })()
}

/**
 * 让启动层退场（3.2.2 的开屏：随机干员立绘 + 名字 + 大招语音 + 进度条）
 *
 * 启动层是 index.html 里的静态层（在 #app 外面），由一段内联脚本驱动 ——
 * 它必须在主包下载**之前**就能显示，所以不能做成 Vue 组件。
 * 这里只负责在挂载完成后通知它：进度补满 → 黄幕擦过 → 淡出 → 移除 → 广播
 * `baker:boot-done`。没有这个对象（旧构建 / 被移除）时静默跳过。
 */
function exitBootLayer(): void {
  const win = window as unknown as { __bakerBoot?: { exit: () => void } }
  win.__bakerBoot?.exit()
}

async function bootstrap(): Promise<void> {  const pinia = createPinia()
  const app = createApp(App)
  app.use(pinia)

  // 全局错误记录:不能依赖 devWarn —— 生产构建会把整个分支 tree-shake 掉,
  // 发布版里的渲染异常就完全没现场可查了。
  app.config.errorHandler = (err, _instance, info) => {
    console.error('[boot] 组件异常:', info, err)
  }

  // 启动恢复:挂载前还原已存数据,避免"初始数据 → 已存数据"首帧跳变。
  // 字体**不**参与这个 await:数据恢复通常远快于 4.3MB 字体,字体放到
  // 挂载后再预热(见 prewarmBubbleFont),弱网首屏不再被字体卡住。
  const chatStore = useChatStore(pinia)
  const settingsStore = useSettingsStore(pinia)
  const { loadProject } = useChatPersistence(chatStore, settingsStore)
  await loadProject()
  // 旧分片兜底：老前端把数据写在 cardsV2/card:N，loadProject 只读 cards。
  // 不兜底的话，老用户在本机存档里只会看到一堆空卡（2026-11-18 实测）。
  await hydrateLegacyCards(chatStore)

  app.mount('#app')
  // 启动层（index.html 里的静态层：随机干员立绘 + 名字 + 大招语音 + 进度）退场。
  // 它在 #app 外面，mount 不会替换它；这里把进度补满、黄幕擦过、淡出再移除，
  // 播完广播 `baker:boot-done` —— 公告/首次偏好要等这个信号才不会隔着黄幕冒出来。
  exitBootLayer()
  prewarmBubbleFont()

  // 账号状态：后台问一次"我是谁"（访客 / 已登录）。**不阻塞挂载** ——
  // 网络慢或无网时聊天界面照样先出来；设置里的「账号」页显示"正在连接"，
  // 打开设置或点「刷新」都会再问一次。
  //
  // 注意：这一问挪到了 App.vue 的 onMounted 里（和「有记录就必须先登录」
  // 那道墙一起做），**这里不要再调一次** —— 连着两问会被 busy 守卫挡掉一半，
  // 结果就是"先查记录数、后知道我是谁"，已登录的人会被误拦一下。
}

// 未捕获的 Promise 异常同样要留现场:这类异常会让界面停在半截,
// 生产环境没有 devError,不记录就完全无法定位。
window.addEventListener('unhandledrejection', (event) => {
  console.error('[boot] 未处理的 Promise 异常:', event.reason)
})

bootstrap().catch((error) => {
  console.error('[boot] 启动失败:', error)
  renderBootError(error)
})
