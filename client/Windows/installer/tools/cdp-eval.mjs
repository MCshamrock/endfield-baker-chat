// cdp-eval.mjs —— 在运行中的应用里执行一段 JS 并打印结果（验收用）
// 用法：node setup\tools\cdp-eval.mjs <port> "<expression>"
const port = process.argv[2] || '9337'
const expr = process.argv[3] || '1+1'

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

await send('Runtime.enable')
const r = await send('Runtime.evaluate', {
  expression: expr,
  returnByValue: true,
  awaitPromise: true,
  userGesture: true,
})
if (r.exceptionDetails) {
  console.error('EXCEPTION:', JSON.stringify(r.exceptionDetails.exception?.description || r.exceptionDetails))
  process.exit(2)
}
console.log(JSON.stringify(r.result.value))
ws.close()
