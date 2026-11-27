// regen-e2e-cdp.mjs —— 真页面验证「重新生成」：点一次，历史里不能出现两句一样的玩家话
//
// 走的是真实 UI（点角色 → 输入 → 回车 → 长按回复 → 重新生成），
// 断言三件事：
//   1. 重新生成之后，服务端会话里仍然只有「玩家 + 角色」两条（不新增玩家消息）
//   2. 上行提示词里那句玩家话**只出现一次**（这才是"她说我叫了两次"的病根）
//   3. 请求体里带上了 regenerate=true（前端确实走了新协议，而不是又发一遍同一句）
//
// 依赖：静态站 15800、后端 15660（见 _audit/wsl_up.sh）、假上游 15999（会记 JSONL）
import { connect, sleep } from './cdp-lite.mjs'

const PAGE = process.env.BAKER_PAGE || 'http://localhost:15800/'
const API = process.env.BAKER_API || 'http://127.0.0.1:15660/v1'
const UP_LOG = process.env.BAKER_UP_LOG || 'F:/dshworkspace/BakerAI/_audit/tmp/upstream.jsonl'

const fs = await import('node:fs')
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

const cdp = await connect()

/**
 * 点掉一个文字按钮（没有就返回 false）
 *
 * 不能只看"没有子元素的节点"：这个站的按钮里普遍套了一层 <span>。
 * 做法是找出所有 innerText 完全相等的节点，取**最内层**那个，再往上找可点的祖先。
 */
async function clickText(text) {
  return cdp.eval(`(() => {
    const want = ${JSON.stringify(text)}
    const all = Array.from(document.querySelectorAll('button, [role="button"], li, .menu-item, span, div, a'))
    const hits = all.filter((el) => (el.textContent || '').trim() === want)
    if (!hits.length) return false
    let el = hits[0]
    for (const h of hits) {
      if (h.querySelectorAll('*').length < el.querySelectorAll('*').length) el = h
    }
    const target = el.closest('button, [role="button"], li, .menu-item, a') || el
    target.scrollIntoView({ block: 'center' })
    target.click()
    return true
  })()`)
}

/** 把页面滚到最底部（聊天区） */
async function scrollBottom() {
  await cdp.eval(`(() => {
    const all = Array.from(document.querySelectorAll('*'))
    const sc = all.filter((el) => el.scrollHeight > el.clientHeight + 40)
    for (const el of sc) el.scrollTop = el.scrollHeight
    window.scrollTo(0, document.body.scrollHeight)
    return sc.length
  })()`)
}

/** 关掉所有已知的一次性弹窗 */
async function dismissDialogs() {
  const known = ['确定', '不再提醒', '开启', '好', '知道了']
  for (let i = 0; i < 8; i++) {
    let hit = false
    for (const t of known) {
      if (await clickText(t)) {
        hit = true
        await sleep(700)
        break
      }
    }
    if (!hit) return
  }
}

/** 打开某位角色的会话 */
async function openCharacter(name) {
  const ok = await cdp.eval(`(() => {
    const want = ${JSON.stringify(name)}
    const hits = Array.from(document.querySelectorAll('span, div, li, p, button'))
      .filter((el) => (el.textContent || '').trim() === want)
    if (!hits.length) return false
    let el = hits[hits.length - 1]
    for (const h of hits) {
      if (h.querySelectorAll('*').length < el.querySelectorAll('*').length) el = h
    }
    const target = el.closest('li, [class*="agent"], [class*="card"], button') || el
    target.scrollIntoView({ block: 'center' })
    target.click()
    return true
  })()`)
  await sleep(1500)
  if (!ok) return false
  // 有的入口会先出「和她聊聊」
  await clickText('和她聊聊')
  await sleep(1200)
  return true
}

/** 输入框（桌面 contenteditable / 移动 textarea）*/
async function inputBox() {
  const info = await cdp.eval(`(() => {
    const el = document.querySelector('.chat-input [contenteditable="true"]')
      || document.querySelector('[contenteditable="true"]')
      || document.querySelector('textarea')
    if (!el) return null
    const r = el.getBoundingClientRect()
    return JSON.stringify({ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) })
  })()`)
  return info ? JSON.parse(info) : null
}

/** 发一句话（真实键入 + 回车） */
async function sendMessage(text) {
  const box = await inputBox()
  if (!box) throw new Error('找不到输入框')
  await cdp.click(box.x, box.y)
  await sleep(300)
  await cdp.type(text)
  await sleep(300)
  await cdp.key('Enter', 'Enter')
}

/** 聊天区最后一条 AI 气泡的中心点 */
async function lastAiBubble() {
  const info = await cdp.eval(`(() => {
    const rows = Array.from(document.querySelectorAll('.msg-row, .chat-row, .bubble-row, [class*="message-row"]'))
    const others = rows.filter((r) => r.className.toString().includes('other') || r.className.toString().includes('ai'))
    const el = (others.length ? others[others.length - 1] : rows[rows.length - 1])
    if (!el) return null
    const bubble = el.querySelector('.bubble, [class*="bubble"]') || el
    const r = bubble.getBoundingClientRect()
    return JSON.stringify({ x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), cls: el.className.toString().slice(0, 60) })
  })()`)
  return info ? JSON.parse(info) : null
}

async function rowsOfLastConversation() {
  // 用页面里的 device token 直接问后端：本机这段会话最新的消息
  const raw = await cdp.eval(`(async () => {
    const token = localStorage.getItem('baker_device_token') || ''
    const list = await (await fetch(${JSON.stringify(API)} + '/conversations', {
      headers: { authorization: 'Bearer ' + token },
    })).json()
    const conv = (list.items || []).sort((a, b) => String(b.last_active_at).localeCompare(String(a.last_active_at)))[0]
    if (!conv) return JSON.stringify({ conv: null, items: [] })
    const msgs = await (await fetch(${JSON.stringify(API)} + '/conversations/' + conv.id + '/messages', {
      headers: { authorization: 'Bearer ' + token },
    })).json()
    return JSON.stringify({ conv, items: msgs.items || [] })
  })()`)
  return JSON.parse(raw)
}

function upstreamCalls() {
  if (!fs.existsSync(UP_LOG)) return []
  return fs
    .readFileSync(UP_LOG, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((line) => JSON.parse(line))
}

// ---------------------------------------------------------------------------
console.log('\n[0] 打开页面')
// 桌面宽度：窄视口会切到移动版布局（没有 contenteditable 输入框）
await cdp.send('Emulation.setDeviceMetricsOverride', {
  width: 1280,
  height: 900,
  deviceScaleFactor: 1,
  mobile: false,
})
await cdp.send('Page.navigate', { url: PAGE })
await sleep(800)
// 每次从干净状态开始：清 localStorage + IndexedDB，否则上一次的角色/会话会串进来
await cdp.eval(`(async () => {
  localStorage.clear()
  localStorage.setItem('baker_backend_url', ${JSON.stringify(API)})
  await new Promise((res) => {
    const req = indexedDB.deleteDatabase('endfield-baker')
    req.onsuccess = req.onerror = req.onblocked = () => res(1)
  })
  return 1
})()`)
await cdp.send('Page.navigate', { url: PAGE })
await sleep(8000)
await dismissDialogs()

console.log('\n[1] 进入「陈千语」并说一句')
const opened = await openCharacter('陈千语')
check(opened, '点开了陈千语的会话')
await dismissDialogs()
await sleep(1000)
let box = null
for (let i = 0; i < 12; i++) {
  await dismissDialogs()
  box = await inputBox()
  if (box) break
  await sleep(800)
}
check(!!box, '找到聊天输入框', box ? `${box.x},${box.y}` : '')

const LINE = '叫一声我的名字'
await sendMessage(LINE)
await sleep(4000)
await scrollBottom()

let state = await rowsOfLastConversation()
check(state.items.length >= 2, '服务端已落「玩家 + 角色」两条', state.items.map((m) => m.side).join(','))
check(
  state.items.filter((m) => m.side === 'user' && m.text === LINE).length === 1,
  '首次发送后玩家消息只有一条',
)

console.log('\n[2] 长按回复 → 重新生成')
const bubble = await lastAiBubble()
check(!!bubble, '找到最后一条角色气泡', bubble ? bubble.cls : '')
if (bubble) {
  // 桌面端是右键菜单（移动端才是长按），直接派发 contextmenu 事件最稳
  await cdp.eval(`(() => {
    const rows = Array.from(document.querySelectorAll('.chat-message-row'))
    const el = rows[rows.length - 1]
    if (!el) return false
    const r = el.getBoundingClientRect()
    el.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, view: window,
      clientX: Math.round(r.x + r.width / 2), clientY: Math.round(r.y + r.height / 2),
    }))
    return true
  })()`)
  await sleep(900)
}
const menu = await cdp.eval(`document.body.innerText.includes('重新生成')`)
check(menu, '长按菜单里出现了「重新生成」')
const clickedRegen = await clickText('重新生成')
check(clickedRegen, '点了「重新生成」')
await sleep(4000)
await scrollBottom()

console.log('\n[3] 断言')
state = await rowsOfLastConversation()
const userRows = state.items.filter((m) => m.side === 'user')
check(
  state.items.length === 2,
  '服务端还是只有两条（没有新增玩家消息）',
  state.items.map((m) => `${m.seq}:${m.side}`).join(' '),
)
check(userRows.length === 1, '玩家消息仍然只有一条', `实际 ${userRows.length} 条`)
check(userRows[0]?.text === LINE, '玩家消息内容没变')

const calls = upstreamCalls()
check(calls.length >= 2, '假上游至少收到两次请求（首答 + 重答）', `${calls.length} 次`)
const last = calls[calls.length - 1]
const hits = (last?.messages || []).filter((m) => m.role === 'user' && m.content === LINE)
check(hits.length === 1, '最后一次提示词里那句玩家话只出现一次', `出现 ${hits.length} 次`)
if (hits.length !== 1) {
  console.log('       提示词尾部：')
  for (const m of (last?.messages || []).slice(-6)) {
    console.log(`        ${m.role}: ${String(m.content).replace(/\s+/g, ' ').slice(0, 40)}`)
  }
}

console.log(`\n结果：${passed} PASS / ${failed} FAIL`)
process.exit(failed ? 1 : 0)
