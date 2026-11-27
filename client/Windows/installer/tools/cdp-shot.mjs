// cdp-shot.mjs —— 通过 CDP 给运行中的 Electron 应用截图（窗口内容，不含桌面遮挡）
// 用法：node setup\tools\cdp-shot.mjs <port> <out.png> [waitMs]
const port = process.argv[2] || '9337'
const out = process.argv[3] || 'shot.png'
const waitMs = Number(process.argv[4] || 1200)

const fs = await import('node:fs/promises')

const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json()
const page = list.find((t) => t.type === 'page')
if (!page) {
  console.error('no page target')
  process.exit(1)
}

const ws = new WebSocket(page.webSocketDebuggerUrl)
let id = 0
const pending = new Map()

function send(method, params = {}) {
  const msgId = ++id
  ws.send(JSON.stringify({ id: msgId, method, params }))
  return new Promise((resolve, reject) => pending.set(msgId, { resolve, reject }))
}

ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data)
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id)
    pending.delete(msg.id)
    if (msg.error) reject(new Error(JSON.stringify(msg.error)))
    else resolve(msg.result)
  }
})

await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve)
  ws.addEventListener('error', reject)
})

await send('Page.enable')
await new Promise((r) => setTimeout(r, waitMs))

const metrics = await send('Page.getLayoutMetrics')
const w = Math.ceil(metrics.cssLayoutViewport?.clientWidth || metrics.layoutViewport.clientWidth)
const h = Math.ceil(metrics.cssLayoutViewport?.clientHeight || metrics.layoutViewport.clientHeight)

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
await fs.writeFile(out, Buffer.from(shot.data, 'base64'))
console.log(`captured ${out} (${w}x${h})`)

const title = await send('Runtime.evaluate', { expression: 'document.title', returnByValue: true })
console.log('title:', title.result.value)
ws.close()
