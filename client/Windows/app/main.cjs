// main.cjs —— Baker AI 4 桌面端主进程
//
// 这是一个**新外壳**：只负责窗口、数据目录、退出落盘、导出另存为，
// 不含任何聊天/角色卡业务逻辑（那些全在网页层 dist 里）。
//
// 安装布局（数据都在安装根，见 paths.cjs）：
//   <安装根>\app\Baker AI 4.exe
//   <安装根>\bakerai_data|logs|webdata|backups|exports
//
// 关窗流程：close 先拦下来 → 给页面发 ns:flush-request → 页面把存档冲刷完
// 回 ns:flush-done 才真正退出（60 秒兜底，避免关不掉）；页面可 cancelClose 取消。

const { app, BrowserWindow, ipcMain, dialog, shell, Menu } = require('electron')
const path = require('path')
const fs = require('fs')
const { DATA_DIRS, resolveRoots } = require('./paths.cjs')

// 版本号只认 package.json：以前这里是写死的字符串，改版本时忘了同步，
// 日志里就会一直报旧版本（4.0.1 的包自报 4.0.0 就是这么来的）。
const APP_VERSION = require('./package.json').version
const APP_NAME = 'Baker AI 4'
const { dataRoot, fallback } = resolveRoots()

// ---- 五个数据目录：启动即建，失败只记日志 ----
const dirs = {}
for (const d of DATA_DIRS) {
  dirs[d] = path.join(dataRoot, d)
  try {
    fs.mkdirSync(dirs[d], { recursive: true })
  } catch {
    /* 只读介质的兜底已在 resolveRoots 里做过 */
  }
}

function logLine(file, text) {
  try {
    fs.appendFileSync(path.join(dirs.bakerai_logs, file), `[${new Date().toISOString()}] ${text}\n`)
  } catch {
    /* 日志失败不影响运行 */
  }
}

logLine(
  'startup.log',
  `${APP_NAME} v${APP_VERSION} start │ electron ${process.versions.electron} │ data=${dataRoot}` +
    (fallback ? ' │ 回退 APPDATA' : ''),
)

process.on('uncaughtException', (err) =>
  logLine('error.log', `uncaughtException: ${err && err.stack ? err.stack : err}`),
)
process.on('unhandledRejection', (err) =>
  logLine('error.log', `unhandledRejection: ${err && err.stack ? err.stack : err}`),
)

// Chromium 的 localStorage / IndexedDB / 缓存也落在安装根（必须在 ready 之前设）
try {
  app.setPath('userData', dirs.bakerai_webdata)
  logLine('startup.log', 'step: userData ok')
} catch (err) {
  logLine('error.log', `setPath userData: ${err}`)
}
app.setAppUserModelId('com.peilika.bakerai')
logLine('startup.log', 'step: appUserModelId ok')

/** 启动备份：每次启动复制一份存档，只留最近 10 份 */
function backupSave() {
  try {
    const src = path.join(dirs.bakerai_data, 'endfield-baker-data.json')
    if (!fs.existsSync(src) || fs.statSync(src).size === 0) return
    const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19)
    fs.copyFileSync(src, path.join(dirs.bakerai_backups, `endfield-baker-data-${ts}.json`))
    const list = fs
      .readdirSync(dirs.bakerai_backups)
      .filter((f) => f.startsWith('endfield-baker-data-') && f.endsWith('.json'))
      .sort()
    while (list.length > 10) {
      const victim = list.shift()
      try {
        fs.unlinkSync(path.join(dirs.bakerai_backups, victim))
      } catch {
        /* 忽略 */
      }
    }
  } catch (err) {
    logLine('error.log', `backup: ${err}`)
  }
}

let win = null
let forceClose = false
let flushPending = false
let flushTimer = null

function createWindow() {
  win = new BrowserWindow({    // 默认窗口 16:9（1440×810 = 1600×900 的 0.9 倍，1080p 屏上正好放得下）
    width: 1440,
    height: 810,
    minWidth: 960,
    minHeight: 540,
    show: false,
    title: APP_NAME,
    backgroundColor: '#141414', // = src/styles/_variables.scss 的 $color-bg-root
    icon: path.join(__dirname, 'icon.ico'),
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false, // preload 要用 fs；渲染进程仍被 contextIsolation 隔离
      spellcheck: false,
    },
  })
  Menu.setApplicationMenu(null)

  win.once('ready-to-show', () => win.show())

  const indexPath = path.join(__dirname, 'dist', 'index.html')
  if (fs.existsSync(indexPath)) {
    win.loadFile(indexPath)
  } else {
    // 打包不完整时给一句人话，不要留一个白窗口
    const html =
      '<meta charset="utf-8"><body style="background:#141414;color:#eee;font:16px sans-serif;padding:40px">' +
      '<h2>Baker AI 4 的文件不完整</h2><p>没有找到 dist/index.html。请重新安装。</p></body>'
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html))
    logLine('error.log', `missing dist/index.html at ${indexPath}`)
  }

  // 外链交给系统浏览器，不在应用内开新窗口
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith('file://')) {
      e.preventDefault()
      if (/^https?:/i.test(url)) shell.openExternal(url)
    }
  })
  win.webContents.on('render-process-gone', (_e, details) => {
    logLine('error.log', `render-process-gone: ${JSON.stringify(details)}`)
  })

  win.on('close', (e) => {
    if (forceClose) return
    e.preventDefault()
    if (flushPending) return
    flushPending = true
    try {
      win.webContents.send('ns:flush-request')
    } catch {
      /* 页面还没加载好 */
    }
    flushTimer = setTimeout(() => {
      logLine('startup.log', 'flush timeout, force close')
      flushPending = false
      forceClose = true
      if (win && !win.isDestroyed()) win.close()
    }, 60_000)
  })
}

ipcMain.on('ns:flush-done', () => {
  if (!flushPending) return
  clearTimeout(flushTimer)
  flushPending = false
  forceClose = true
  if (win && !win.isDestroyed()) win.close()
})

ipcMain.on('ns:cancel-close', () => {
  if (!flushPending) return
  clearTimeout(flushTimer)
  flushPending = false
})

// ---- 另存为（导出 zip / JSON）----
ipcMain.handle('ns:save-dialog', async (_e, opts) => {
  const rawName = String((opts && opts.fileName) || 'export.zip')
  const fileName = path.basename(rawName).replace(/[<>:"/\\|?*\x00-\x1f]/g, '_') || 'export.zip'
  try {
    fs.mkdirSync(dirs.bakerai_exports, { recursive: true })
  } catch {
    /* 忽略 */
  }
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title: '另存为',
    defaultPath: path.join(dirs.bakerai_exports, fileName),
    filters: fileName.toLowerCase().endsWith('.zip')
      ? [{ name: 'ZIP 压缩包', extensions: ['zip'] }]
      : [{ name: 'JSON 数据', extensions: ['json'] }],
  })
  if (canceled || !filePath) return { canceled: true }
  try {
    const buf = Buffer.from(String((opts && opts.base64) || ''), 'base64')
    fs.writeFileSync(filePath, buf)
    const back = fs.readFileSync(filePath)
    if (back.length !== buf.length) return { canceled: false, path: filePath, ok: false }
    logLine('startup.log', `saveFileDialog ok: ${filePath} (${buf.length} B)`)
    return { canceled: false, path: filePath }
  } catch (err) {
    logLine('error.log', `saveFileDialog fail: ${err}`)
    return { canceled: false, path: filePath, ok: false, error: String(err) }
  }
})

/**
 * 自动冒烟（只在本机 QA 时用：启动前设 BAKER_SMOKE=1）
 *
 * 载入后等 6 秒（启动层 / 首屏动画走完），检查渲染层、原生桥、后端连通性，
 * 截一张窗口图，写进 <数据根>\smoke\，然后直接退出。正式用户不会触发。
 */
function runSmoke() {
  const outDir = path.join(dataRoot, 'smoke')
  fs.mkdirSync(outDir, { recursive: true })
  logLine('startup.log', 'smoke requested')
  win.webContents.once('did-finish-load', async () => {
    logLine('startup.log', 'smoke did-finish-load')
    try {
      await new Promise((resolve) => setTimeout(resolve, 6000))
      const info = await win.webContents.executeJavaScript(`(async () => {
        let metaStatus = 0
        let metaError = ''
        try {
          const r = await fetch('https://chat.peilika.beer/v1/meta')
          metaStatus = r.status
        } catch (e) { metaError = String(e) }
        return {
          title: document.title,
          href: location.href,
          bridge: !!window.nativeStorage,
          hasApp: !!document.querySelector('#app'),
          bootVisible: !!document.querySelector('.boot'),
          bodyText: (document.body.innerText || '').slice(0, 400),
          metaStatus,
          metaError,
        }
      })()`)
      fs.writeFileSync(path.join(outDir, 'smoke.json'), JSON.stringify(info, null, 2), 'utf8')
      const image = await win.webContents.capturePage()
      fs.writeFileSync(path.join(outDir, 'smoke.png'), image.toPNG())

      // 第二段（BAKER_SMOKE_UI=1）：走一遍「设置 → 数据管理 → 从旧版导入」，
      // 确认新加的入口真的在界面上，而不是只存在于源码里。
      if (process.env.BAKER_SMOKE_UI === '1') {
        const ui = await win.webContents.executeJavaScript(`(async () => {
          const wait = (ms) => new Promise((r) => setTimeout(r, ms))
          const clickText = (text) => {
            const el = [...document.querySelectorAll('button')]
              .find((b) => (b.textContent || '').trim().includes(text))
            if (el) { el.click(); return true }
            return false
          }
          await wait(300)
          // 服务端公告会挡在首页：先关掉（点「确定」或「不再提醒」都行）
          const closed = clickText('确定') || clickText('不再提醒')
          await wait(600)
          const settingsBtn = document.querySelector('[aria-label="设置"]')
          if (settingsBtn) settingsBtn.click()
          await wait(700)
          const dataTab = [...document.querySelectorAll('button[role="tab"]')]
            .find((b) => (b.textContent || '').trim() === '数据')
          if (dataTab) dataTab.click()
          await wait(700)
          const importBtn = [...document.querySelectorAll('button')]
            .find((b) => (b.textContent || '').includes('从旧版导入'))
          const aboutTab = [...document.querySelectorAll('button[role="tab"]')]
            .find((b) => (b.textContent || '').trim() === '关于')
          return {
            noticeClosed: closed,
            settingsOpened: !!document.querySelector('button[role="tab"]'),
            dataTabFound: !!dataTab,
            importBtnFound: !!importBtn,
            importBtnText: importBtn ? importBtn.textContent.trim() : '',
            aboutTabFound: !!aboutTab,
            aboutText: aboutTab ? aboutTab.textContent.trim() : '',
          }
        })()`)
        fs.writeFileSync(path.join(outDir, 'smoke-ui.json'), JSON.stringify(ui, null, 2), 'utf8')
        const shot = await win.webContents.capturePage()
        fs.writeFileSync(path.join(outDir, 'smoke-ui.png'), shot.toPNG())
        logLine('startup.log', `smoke ui ${JSON.stringify(ui)}`)
      }

      logLine('startup.log', `smoke ok ${JSON.stringify(info)}`)
    } catch (err) {
      logLine('error.log', `smoke fail: ${err && err.stack ? err.stack : err}`)
    } finally {
      app.exit(0)
    }
  })
  // 兜底：页面一直没加载完也要留下证据并退出
  setTimeout(() => {
    try {
      fs.writeFileSync(path.join(outDir, 'smoke.json'), JSON.stringify({ timeout: true }), 'utf8')
      logLine('startup.log', 'smoke timeout')
    } catch {}
    app.exit(0)
  }, 20000)
}

// 单实例：第二个实例把已有窗口提到前面就好
if (!app.requestSingleInstanceLock()) {  app.quit()
} else {
  app.on('second-instance', () => {
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore()
      win.focus()
    }
  })

  logLine('startup.log', 'step: before whenReady')
  app.whenReady().then(() => {
    logLine('startup.log', 'step: ready')
    // setAppLogsPath 必须在 ready 之后调用（提前调会触发 Chromium CHECK，进程直接崩）
    try {
      app.setAppLogsPath(dirs.bakerai_logs)
    } catch (err) {
      logLine('error.log', `setAppLogsPath: ${err}`)
    }
    backupSave()
    createWindow()
    if (process.env.BAKER_SMOKE === '1') runSmoke()
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  })

  app.on('window-all-closed', () => app.quit())
  app.on('before-quit', () => {
    if (flushTimer) clearTimeout(flushTimer)
  })
}
