// cdp-storage-check.mjs —— 用 Edge(CDP) 验证「本地优先」的存储链路
//
// 验的是**接线**，不是纯函数（纯函数在 scripts/list-e2e.mjs 里已测）：
//   1. 首次进入默认「本机」存储（baker_list_source 没有 / 是 local）
//   2. 本机为空时，云端有的会话会被补进本机，并**真的写进 IndexedDB**
//   3. 切到「本机 + 云端」模式后，再补一次仍然只加不改，且本机的会话没丢
//
// 用法：node _audit/cdp-storage-check.mjs
// 依赖：Edge(9333 CDP) 已开、静态站 15800、后端 15660（见 _audit/wsl_live.sh）

const CDP = process.env.BAKER_CDP || 'http://127.0.0.1:9333'
const PAGE = process.env.BAKER_PAGE || 'http://localhost:15800/'
const API = process.env.BAKER_API || 'http://127.0.0.1:15660/v1'

let passed = 0
let failed = 0
function check(cond, name, detail = '') {
  if (cond) {
    passed += 1
    console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`)
  } else {
    failed += 1
    console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`)
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function pageTarget() {
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

class Cdp {
  constructor(ws) {
    this.ws = ws
    this.seq = 0
    this.waiting = new Map()
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      const hit = this.waiting.get(msg.id)
      if (hit) {
        this.waiting.delete(msg.id)
        msg.error ? hit.reject(new Error(JSON.stringify(msg.error))) : hit.resolve(msg.result)
      }
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
}

const target = await pageTarget()
const ws = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve)
  ws.addEventListener('error', reject)
})
const cdp = new Cdp(ws)
await cdp.send('Runtime.enable')
await cdp.send('Page.enable')

/** 打开页面（带后端地址覆盖）并等启动完成 */
async function openApp() {
  // 第一次先落到站点 origin 上，否则 localStorage 写在 about:blank 的隔离身位里，白写
  await cdp.send('Page.navigate', { url: PAGE })
  await sleep(1500)
  await cdp.eval(`localStorage.setItem('baker_backend_url', ${JSON.stringify(API)}), 1`)
  await cdp.send('Page.navigate', { url: PAGE })
  await sleep(9000)
}

/** 读 IndexedDB 里的卡片树（真实落盘的那一份） */
const READ_CARDS = `(async () => {
  const open = () => new Promise((res, rej) => {
    const r = indexedDB.open('endfield-baker')
    r.onsuccess = () => res(r.result)
    r.onerror = () => rej(r.error)
  })
  const db = await open()
  const get = (k) => new Promise((res, rej) => {
    const t = db.transaction('data', 'readonly')
    const q = t.objectStore('data').get(k)
    q.onsuccess = () => res(q.result)
    q.onerror = () => rej(q.error)
  })
  const cards = await get('cards')
  db.close()
  return JSON.stringify(cards || [])
})()`

async function readCards() {
  const raw = await cdp.eval(READ_CARDS)
  return JSON.parse(raw)
}

/** 汇总：卡数 / 会话数 / 所有 remoteId */
function summarize(cards) {
  const convs = cards.flatMap((c) => c.conversations || [])
  return {
    cards: cards.length,
    convs: convs.length,
    remoteIds: convs.map((c) => c.remoteId).filter(Boolean),
    names: convs.map((c) => c.name),
  }
}

/** 直接从页面里拿设备 token（前端自己存在 localStorage） */
async function deviceToken() {
  return cdp.eval(
    `localStorage.getItem('baker_device_token') || localStorage.getItem('baker_token') || ''`,
  )
}

async function apiCreateConversation(token, title) {
  const res = await fetch(`${API}/conversations`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
    body: JSON.stringify({ character_id: 'perlica', mode: 'single', title }),
  })
  if (!res.ok) throw new Error(`建会话失败 ${res.status}: ${await res.text()}`)
  return (await res.json()).id
}

// ---------------------------------------------------------------------------

console.log('\n[1] 首次进入：默认「本机」')
await openApp()
const sourceDefault = await cdp.eval(`localStorage.getItem('baker_list_source')`)
check(sourceDefault === 'local', '首次进入默认本机存储', String(sourceDefault))
const before = summarize(await readCards())
console.log(`       本机现状：${before.cards} 张卡 / ${before.convs} 段会话`)

const token = await deviceToken()
check(!!token, '设备 token 已落 localStorage', token ? `${token.slice(0, 10)}…` : '空')

console.log('\n[2] 服务端有、本机没有 → 自动补进本机（并写盘）')
const convId = await apiCreateConversation(token, '佩丽卡')
console.log(`       服务端新建会话：${convId}`)
// 本机为空 → 重载时 ensureLoaded() 会走 hydrateIfEmpty()
await cdp.send('Page.navigate', { url: PAGE })
await sleep(9000)
let after = summarize(await readCards())
check(after.remoteIds.includes(convId), '云端会话被我补进了本机卡库（IndexedDB）', convId)
check(after.convs >= before.convs, '本机会话数没有减少', `${before.convs} → ${after.convs}`)

console.log('\n[3] 切「本机 + 云端」：再补一次仍然只加不改')
await cdp.eval(`localStorage.setItem('baker_list_source', 'server'), 1`)
const convId2 = await apiCreateConversation(token, '佩丽卡')
await cdp.send('Page.navigate', { url: PAGE })
await sleep(9000)
const after2 = summarize(await readCards())
check(after2.remoteIds.includes(convId2), '模式切到云端后，新会话也被补进本机', convId2)
check(
  after2.remoteIds.includes(convId) && after2.remoteIds.includes(convId2),
  '两次补进来的会话**同时**在本地（只加不改）',
  after2.remoteIds.length + ' 段带 remoteId',
)
check(
  after2.convs >= after.convs,
  '本地会话数只增不减',
  `${after.convs} → ${after2.convs}`,
)

console.log('\n[4] 切回「本机」：记录照旧在（不再依赖服务端）')
await cdp.eval(`localStorage.setItem('baker_list_source', 'local'), 1`)
// 断掉云端（把后端地址指向一个打不通的端口），只靠本机渲染
await cdp.eval(`localStorage.setItem('baker_backend_url', 'http://127.0.0.1:15699/v1'), 1`)
await cdp.send('Page.navigate', { url: PAGE })
await sleep(9000)
const after3 = summarize(await readCards())
check(
  after3.remoteIds.includes(convId) && after3.remoteIds.includes(convId2),
  '后端打不通时，本机照样有这两段会话（本地优先的意义）',
  after3.remoteIds.length + ' 段带 remoteId',
)
const bodyText = await cdp.eval(`document.body.innerText.slice(0, 400)`)
check(typeof bodyText === 'string' && bodyText.length > 0, '页面正常渲染（有文字）')

// 收尾：把后端地址改回去，别把调试配置留在浏览器里
await cdp.eval(`localStorage.setItem('baker_backend_url', ${JSON.stringify(API)}), 1`)

console.log(`\n结果：${passed} PASS / ${failed} FAIL`)
ws.close()
process.exit(failed === 0 ? 0 : 1)
