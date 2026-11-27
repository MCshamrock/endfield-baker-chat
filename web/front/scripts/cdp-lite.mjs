// cdp-lite.mjs —— 极小的 CDP 客户端（无依赖，Node 22 自带 WebSocket）
// 供本次「重新生成」取证脚本复用。
const CDP = process.env.BAKER_CDP || 'http://127.0.0.1:9333'

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

export async function pageTarget() {
  for (let i = 0; i < 40; i++) {
    try {
      const list = await (await fetch(`${CDP}/json/list`)).json()
      const page = list.find((t) => t.type === 'page')
      if (page?.webSocketDebuggerUrl) return page
    } catch {
      // Edge 还没起来
    }
    await sleep(500)
  }
  throw new Error('连不上 Edge 的 CDP（9333）')
}

export class Cdp {
  constructor(ws) {
    this.ws = ws
    this.seq = 0
    this.waiting = new Map()
    this.events = []
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id) {
        const hit = this.waiting.get(msg.id)
        if (hit) {
          this.waiting.delete(msg.id)
          msg.error ? hit.reject(new Error(JSON.stringify(msg.error))) : hit.resolve(msg.result)
        }
        return
      }
      this.events.push(msg)
    })
  }
  send(method, params = {}) {
    const id = ++this.seq
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async eval(expression, awaitPromise = true) {
    const r = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise,
      returnByValue: true,
      userGesture: true,
    })
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text || 'eval 失败')
    return r.result?.value
  }
  /** 鼠标点击（真实输入事件，走 Vue 的 @click） */
  async click(x, y) {
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 })
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  }
  /** 长按（打开消息操作菜单） */
  async longPress(x, y, ms = 650) {
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 })
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
    await sleep(ms)
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  }
  async type(text) {
    await this.send('Input.insertText', { text })
  }
  async key(key, code) {
    await this.send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode: key === 'Enter' ? 13 : 0 })
    await this.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: key === 'Enter' ? 13 : 0 })
  }
}

export async function connect() {
  const target = await pageTarget()
  const ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve)
    ws.addEventListener('error', reject)
  })
  const cdp = new Cdp(ws)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  return cdp
}
