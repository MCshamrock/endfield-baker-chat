// preload.cjs —— 给渲染进程注入最小文件能力 window.nativeStorage
//
// 网页层只认这一组接口（见 src/utils/nativeStorage.ts、nativeExport.ts、
// App.vue 的退出握手）：
//   readFile / writeFile / exists / writeFileChunked   存档读写
//   saveFileDialog                                     导出「另存为」
//   flushDone / cancelClose                            关窗握手回执
//
// 安全边界：contextIsolation 开着，渲染进程拿不到 Node；这里只暴露上面几个函数，
// 且文件名一律取 basename —— 渲染层无法用 ../ 读到数据目录以外的东西。

const { contextBridge, ipcRenderer } = require('electron')
const path = require('path')
const fs = require('fs')
const { resolveRoots } = require('./paths.cjs')

const { dataRoot } = resolveRoots()
const DATA_DIR = path.join(dataRoot, 'bakerai_data')

/** 只允许读写数据目录下的平铺文件，挡住 ../ 之类的路径 */
function safePath(name) {
  const base = path.basename(String(name || ''))
  if (!base || base === '.' || base === '..') throw new Error('非法文件名')
  return path.join(DATA_DIR, base)
}

function ensureDir() {
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true })
    return true
  } catch {
    return false
  }
}

const bridge = {
  /** 读整文件；不存在或读失败返回 null（调用方据此区分「空库」与「读不到」） */
  async readFile(name) {
    try {
      const p = safePath(name)
      if (!fs.existsSync(p)) return null
      return fs.readFileSync(p, 'utf8')
    } catch {
      return null
    }
  },

  /** 文件是否存在；用于判断「首次使用」而不是「读取失败」 */
  async exists(name) {
    try {
      return fs.existsSync(safePath(name))
    } catch {
      return false
    }
  },

  /** 覆盖写；写完回读比对长度，返回 false 表示没真正落盘（磁盘满/被占用） */
  async writeFile(name, content) {
    try {
      if (!ensureDir()) return false
      const p = safePath(name)
      const text = String(content ?? '')
      fs.writeFileSync(p, text, 'utf8')
      if (fs.statSync(p).size !== Buffer.byteLength(text, 'utf8')) return false
      return true
    } catch {
      return false
    }
  },

  /** 分块写：先写 .tmp 再 rename，强杀时不会留下半截主文件 */
  async writeFileChunked(name, content) {
    try {
      if (!ensureDir()) return false
      const p = safePath(name)
      const tmp = p + '.tmp'
      const text = String(content ?? '')
      const CHUNK = 256 * 1024
      const fd = fs.openSync(tmp, 'w')
      try {
        for (let i = 0; i < text.length; i += CHUNK) {
          let end = Math.min(i + CHUNK, text.length)
          const last = text.charCodeAt(end - 1)
          // 不要把 UTF-16 代理对劈成两半
          if (end < text.length && last >= 0xd800 && last <= 0xdbff) end -= 1
          fs.writeSync(fd, Buffer.from(text.slice(i, end), 'utf8'))
          i = end - CHUNK
        }
      } finally {
        fs.closeSync(fd)
      }
      if (fs.statSync(tmp).size !== Buffer.byteLength(text, 'utf8')) return false
      fs.renameSync(tmp, p)
      return true
    } catch {
      return false
    }
  },

  /** 导出：交给主进程弹系统「另存为」框 */
  async saveFileDialog(opts) {
    return ipcRenderer.invoke('ns:save-dialog', {
      fileName: String((opts && opts.fileName) || 'export.zip'),
      base64: String((opts && opts.base64) || ''),
    })
  },

  /** 关窗握手：落盘完成 */
  flushDone() {
    ipcRenderer.send('ns:flush-done')
  },

  /** 关窗握手：玩家取消了退出 */
  cancelClose() {
    ipcRenderer.send('ns:cancel-close')
  },
}

contextBridge.exposeInMainWorld('nativeStorage', bridge)

// 主进程的关窗请求 → 页面事件（App.vue 监听 dsh-flush-request）
ipcRenderer.on('ns:flush-request', () => {
  window.dispatchEvent(new CustomEvent('dsh-flush-request'))
})
