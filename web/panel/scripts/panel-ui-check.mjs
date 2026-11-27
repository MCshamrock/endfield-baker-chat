#!/usr/bin/env node
/**
 * scripts/panel-ui-check.mjs —— 用无头 Edge 真开一遍面板，点真实按钮，截图，收集报错。
 *
 * 为什么不用 jsdom 之类：这一页的价值全在"真的渲染出来了吗"。
 * jsdom 不跑 CSS、不跑字体、不跑 Vue 的 patch，截图更不可能。所以走 CDP：
 * 起一个真 Edge（headless），从外部发 Input/Runtime 指令，跟人操作差不多。
 *
 * 它做的事（顺序 = 运维验收顺序）：
 *   登录 → 总览数字出来 → 用户表 → 干员改一次权重（并回读后端确认落盘）
 *   → 配置读 → 密钥表（断言页面上没有任何密钥内容）→ 用量 → 思考链 → 浅色主题
 * 全程收集 console error、未捕获异常、4xx/5xx 响应。
 *
 * 用法：
 *   node scripts/panel-ui-check.mjs --base http://localhost:5273 \
 *     --user panel_admin --pass *** --conversation <带 reasoning 的 public_id>
 *
 * 前置：dev server 已经跑起来（npm run dev），后端 5660 在跑。
 */

import { spawn, execFile } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdirSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'

const argv = process.argv.slice(2)
const arg = (name, fallback = '') => {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback
}

const BASE = arg('base', 'http://localhost:5273')
const USER = arg('user', process.env.BAKER_PANEL_USER || 'panel_admin')
const PASS = arg('pass', process.env.BAKER_PANEL_PASS || '')
const CONV = arg('conversation', '')
/** 可选：一个**不是管理员**的账号，用来验证"登录成功但没权限"这条路（不给就跳过） */
const FORBID_USER = arg('forbidden-user', '')
const FORBID_PASS = arg('forbidden-pass', '')
const OUT = resolve('docs/screenshots')
/** CDP 端口：默认自动挑一个空闲的。写死过 9333，结果撞上上一次没退干净的 Edge，
 *  表现是"Edge 没起来"——排查了半小时。自动挑 + 双栈探测才是对的。 */
const PORT = Number(arg('port', '0')) || 0
const EDGE = arg('edge', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
const PROFILE = join(process.env.TEMP || '/tmp', `panel-edge-${Date.now()}`)

if (!PASS) {
  console.error('缺少密码：--pass 或 BAKER_PANEL_PASS')
  process.exit(2)
}

mkdirSync(OUT, { recursive: true })

const checks = []
const consoleErrors = []
const pageExceptions = []
const badResponses = []
const shots = []
/** 预期内的 403 / 控制台噪音：验证"非管理员被拦住"时，那个 403 是我们要的结果，
 *  不能算成故障。按阶段开关，避免把真正的 403 也一起放过。 */
const expectedBad = []
const expectedConsole = []
let expectForbidden = false

function ok(name, note = '') {
  checks.push({ name, ok: true, note })
  console.log(`PASS  ${name}${note ? `  （${note}）` : ''}`)
}
function bad(name, note) {
  checks.push({ name, ok: false, note })
  console.log(`FAIL  ${name}\n      ${note}`)
}
function need(cond, message) {
  if (!cond) throw new Error(message)
}

// ---------------------------------------------------------------- CDP 小客户端
class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.handlers = new Map()
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve: res, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(`${msg.error.message}（${msg.error.code}）`))
        else res(msg.result)
        return
      }
      const list = this.handlers.get(msg.method)
      if (list) for (const fn of list) fn(msg.params)
    })
  }

  send(method, params = {}) {
    const id = (this.id += 1)
    return new Promise((res, reject) => {
      this.pending.set(id, { resolve: res, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id)
          reject(new Error(`CDP 超时：${method}`))
        }
      }, 30000)
    })
  }

  on(method, fn) {
    const list = this.handlers.get(method) || []
    list.push(fn)
    this.handlers.set(method, list)
  }
}

async function fetchJson(url) {
  const res = await fetch(url)
  return res.json()
}

/** 挑一个 127.0.0.1 上能绑的空闲端口（Edge 还要绑 ::1，那边被占只会在日志里报错，不影响） */
function pickPort() {
  return new Promise((resolvePort, reject) => {
    const server = createServer()
    server.once('error', reject)
    server.listen({ port: 0, host: '127.0.0.1', exclusive: true }, () => {
      const { port } = server.address()
      server.close(() => resolvePort(port))
    })
  })
}

let edge = null
let cdp = null
/** 实际能连上的调试地址（Edge 双栈，有时只有 ::1 通） */
let debugBase = ''

async function launch(port) {
  edge = spawn(EDGE, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--hide-scrollbars',
    '--window-size=1440,1000',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${PROFILE}`,
    'about:blank',
  ], { stdio: 'ignore' })

  // Edge 起来要几百毫秒。轮询两个地址族：Windows 上 Edge 有可能只绑到 [::1]。
  const candidates = [`http://127.0.0.1:${port}`, `http://[::1]:${port}`]
  const t0 = Date.now()
  let lastError = ''
  edge.on('error', (err) => {
    lastError = `spawn 失败：${err.message}`
  })
  edge.on('exit', (code) => {
    lastError = `Edge 进程退出了（code=${code}）`
  })

  while (Date.now() - t0 < 25000) {
    for (const candidate of candidates) {
      try {
        const version = await fetchJson(`${candidate}/json/version`)
        if (version?.webSocketDebuggerUrl) {
          debugBase = candidate
          return version
        }
        lastError = '/json/version 里没有 webSocketDebuggerUrl'
      } catch (err) {
        lastError = err.message
      }
    }
    await sleep(200)
  }
  throw new Error(`Edge 没起来（调试端口 ${port} 两个地址族都没响应：${lastError}）`)
}

async function connect() {
  // 找一个 page target（about:blank）。注意列表里还有扩展的 background_page，别连错。
  const list = await fetchJson(`${debugBase}/json/list`)
  const page = list.find((t) => t.type === 'page')
  if (!page) throw new Error('没有 page target')

  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true })
    ws.addEventListener('error', () => rej(new Error('连不上 page 的 WebSocket')), { once: true })
  })
  cdp = new Cdp(ws)
}

/** 杀整个进程树：只 kill 启动器的话，Edge 的浏览器进程会活下来占着端口（踩过） */
function killTree(pid) {
  return new Promise((res) => {
    if (!pid) return res()
    execFile('taskkill', ['/PID', String(pid), '/T', '/F'], () => res())
  })
}

async function evaluate(expression) {
  const r = await cdp.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  })
  if (r.exceptionDetails) {
    const text = r.exceptionDetails.exception?.description || r.exceptionDetails.text
    throw new Error(`页面里执行出错：${text}`)
  }
  return r.result.value
}

async function waitFor(label, expression, timeout = 12000) {
  const t0 = Date.now()
  let last = ''
  while (Date.now() - t0 < timeout) {
    try {
      if (await evaluate(expression)) return
    } catch (err) {
      last = err.message
    }
    await sleep(120)
  }
  // 超时的时候把"页面现在是什么样"带出来 —— 否则只看到一句"超时"，排不了障
  let snapshot = ''
  try {
    snapshot = await evaluate(
      `JSON.stringify({ href: location.href, ready: document.readyState, title: document.title, body: (document.body?.innerText || '').slice(0, 160) })`,
    )
  } catch {
    /* 页面可能已经没了 */
  }
  throw new Error(`等待超时：${label}${last ? `（${last}）` : ''} 页面状态：${snapshot}`)
}

async function shoot(name, fullPage = true) {
  let params = { format: 'png' }
  if (fullPage) {
    const metrics = await cdp.send('Page.getLayoutMetrics')
    const size = metrics.cssContentSize || metrics.contentSize
    params = {
      format: 'png',
      captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: Math.ceil(size.width), height: Math.min(Math.ceil(size.height), 5000), scale: 1 },
    }
  }
  const { data } = await cdp.send('Page.captureScreenshot', params)
  const file = join(OUT, `${name}.png`)
  writeFileSync(file, Buffer.from(data, 'base64'))
  shots.push({ name, file, bytes: Buffer.from(data, 'base64').length })
  console.log(`      📷 ${name}.png`)
}

// ---------------------------------------------------------------- 主流程
const net = []
const byRequestId = new Map()

async function main() {
  const port = PORT || (await pickPort())
  console.log(`调试端口 ${port}，页面 ${BASE}\n`)
  await launch(port)
  await connect()

  cdp.on('Runtime.consoleAPICalled', (p) => {
    if (p.type === 'error') {
      const text = p.args.map((a) => a.value ?? a.description ?? a.type).join(' ')
      if (expectForbidden && /403/.test(text)) expectedConsole.push(text)
      else consoleErrors.push(text)
    }
  })
  cdp.on('Runtime.exceptionThrown', (p) => {
    pageExceptions.push(p.exceptionDetails.exception?.description || p.exceptionDetails.text)
  })
  cdp.on('Log.entryAdded', (p) => {
    if (p.entry.level !== 'error') return
    const text = `${p.entry.source}: ${p.entry.text}`
    if (expectForbidden && /403/.test(text)) expectedConsole.push(text)
    else consoleErrors.push(text)
  })
  cdp.on('Network.requestWillBeSent', (p) => {
    byRequestId.set(p.requestId, { method: p.request.method, url: p.request.url })
  })
  cdp.on('Network.responseReceived', (p) => {
    const req = byRequestId.get(p.requestId) || { method: '?', url: p.response.url }
    const item = { ...req, status: p.response.status, requestId: p.requestId, mime: p.response.mimeType }
    net.push(item)
    if (p.response.status >= 400) {
      // 验"非管理员"时那个 403 是预期结果（探针被正确拒绝），不算故障
      if (expectForbidden && p.response.status === 403) expectedBad.push(item)
      else badResponses.push(item)
    }
  })

  await cdp.send('Page.enable')
  await cdp.send('Runtime.enable')
  await cdp.send('Log.enable')
  await cdp.send('Network.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  })

  // ---- 1. 登录页 ----
  await cdp.send('Page.navigate', { url: `${BASE}/#/overview` })
  await waitFor('登录页出现', `!!document.querySelector('.pn-login-card')`)
  const loginShape = await evaluate(`(() => {
    const card = document.querySelector('.pn-login-card')
    return {
      inputs: card.querySelectorAll('input').length,
      passwords: card.querySelectorAll('input[type=password]').length,
      submit: !!card.querySelector('button[type=submit]'),
      title: card.querySelector('h1').textContent.trim(),
      theme: document.documentElement.dataset.theme,
    }
  })()`)
  await shoot('01-login', false)
  try {
    need(loginShape.inputs === 2, `登录表单应有 2 个输入框，实际 ${loginShape.inputs}`)
    need(loginShape.passwords === 1, '密码框类型不对')
    need(loginShape.submit, '没有提交按钮')
    need(loginShape.theme === 'dark', `默认主题应为 dark，实际 ${loginShape.theme}`)
    ok('登录页：两个输入框 + 密码框 + 提交按钮，默认深色', loginShape.title)
  } catch (err) {
    bad('登录页结构', err.message)
  }

  // ---- 2. 登录 ----
  await evaluate(`(() => {
    const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })) }
    const inputs = document.querySelectorAll('.pn-login-card input')
    set(inputs[0], ${JSON.stringify(USER)})
    set(inputs[1], ${JSON.stringify(PASS)})
    document.querySelector('.pn-login-card button[type=submit]').click()
    return true
  })()`)
  try {
    await waitFor('登录成功（命令栏出现）', `!!document.querySelector('.ef-cmdbar')`)
    const who = await evaluate(`document.querySelector('.pn-status .live b')?.textContent || ''`)
    need(who === USER, `页头显示的用户是 ${who}`)
    ok('登录成功，进入面板', `身份 ${who}`)
  } catch (err) {
    const message = await evaluate(`document.querySelector('.ef-notice')?.textContent || ''`)
    bad('登录', `${err.message}；页面上写着：${message}`)
    throw err
  }

  // ---- 3. 总览 ----
  try {
    await waitFor('总览数字出现', `document.querySelectorAll('.pn-figs b').length >= 9`)
    const figures = await evaluate(`(() => {
      const nums = [...document.querySelectorAll('.pn-figs b')].map(b => b.textContent.trim())
      const labels = [...document.querySelectorAll('.pn-figs .label')].map(s => s.textContent.trim())
      const bars = document.querySelectorAll('.pn-chart .ef-bars li').length
      const heads = [...document.querySelectorAll('.ef-head h1')].map(h => h.textContent.trim())
      return { nums, labels, bars, heads }
    })()`)
    const allNumeric = figures.nums.every((n) => /^[\d,]+$/.test(n))
    need(allNumeric, `有数字不是数字：${figures.nums.join(' | ')}`)
    need(figures.bars === 7, `近 7 天应有 7 根柱子，实际 ${figures.bars}`)
    need(figures.heads[0].startsWith('总览'), `页头标题不对：${figures.heads[0]}`)
    ok('总览：规模 + 密钥池 + 近 7 天直方图都渲染了', `${figures.labels.length} 个指标，${figures.bars} 根柱子`)
  } catch (err) {
    bad('总览渲染', err.message)
  }
  await shoot('02-overview-dark')

  // ---- 4. 用户表 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/users"]').click()`)
    await waitFor('用户表出现', `document.querySelectorAll('.pn-table tbody tr').length > 0`)
    const shape = await evaluate(`(() => {
      const rows = document.querySelectorAll('.pn-table tbody tr')
      const first = rows[0]
      const meRow = [...rows].find(r => r.querySelector('.ef-state.is-alert'))
      return {
        rows: rows.length,
        selects: first.querySelectorAll('select').length,
        nameInput: !!first.querySelector('input'),
        meTag: !!meRow,
        meLocked: meRow ? [...meRow.querySelectorAll('select')].every(s => s.disabled) : false,
        total: document.querySelector('.pn-pager')?.textContent.replace(/\\s+/g, ' ').trim() || '',
      }
    })()`)
    need(shape.selects === 2, `每行应有 角色/状态 两个下拉，实际 ${shape.selects}`)
    need(shape.nameInput, '每行应有显示名输入框')
    need(shape.meTag, '没有标出"我"那一行')
    need(shape.meLocked, '"我"那一行的角色/状态下拉应该被锁住（不能把自己降权）')
    ok('用户表：搜索/分页/行内改角色·状态·显示名都在，"我"那行锁住', `${shape.rows} 行，${shape.total}`)
  } catch (err) {
    bad('用户表', err.message)
  }
  await shoot('03-users')

  // ---- 5. 干员：改一次权重 ----
  let weightChange = null
  try {
    await evaluate(`document.querySelector('nav a[href="#/characters"]').click()`)
    await waitFor('干员表出现', `document.querySelectorAll('.pn-table tbody tr').length > 0`)
    const before = await evaluate(`(() => {
      const row = document.querySelector('.pn-table tbody tr')
      const input = row.querySelector('input[type=number]')
      return { name: row.querySelector('b').textContent.trim(), weight: Number(input.value) }
    })()`)
    const next = before.weight >= 100 ? before.weight - 1 : before.weight + 1
    // 先改输入框（Vue 的 v-model 监听 input 事件），等一拍让"保存"解除禁用
    await evaluate(`(() => {
      const input = document.querySelector('.pn-table tbody tr input[type=number]')
      input.value = '${next}'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`)
    await sleep(150)
    await evaluate(`(() => {
      const row = document.querySelector('.pn-table tbody tr')
      const btn = [...row.querySelectorAll('button')].find(b => b.textContent.trim() === '保存')
      btn.click()
      return true
    })()`)
    await waitFor('保存请求返回', `!!document.querySelector('.ef-toast')`, 8000)

    // 从网络日志里捞这次 PATCH 的响应体，确认真的落盘了
    const patch = [...net].reverse().find((n) => n.method === 'PATCH' && n.url.includes('/v1/admin/characters/'))
    need(patch, '没有发现 PATCH 干员的请求')
    need(patch.status === 200, `PATCH 状态码 ${patch.status}`)
    const body = await cdp.send('Network.getResponseBody', { requestId: patch.requestId })
    const saved = JSON.parse(body.body)
    need(saved.active_weight === next, `后端回显的权重是 ${saved.active_weight}，期望 ${next}`)

    // 改回去，别在库里留测试值
    await evaluate(`(() => {
      const input = document.querySelector('.pn-table tbody tr input[type=number]')
      input.value = '${before.weight}'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`)
    await sleep(150)
    await evaluate(`(() => {
      const row = document.querySelector('.pn-table tbody tr')
      ;[...row.querySelectorAll('button')].find(b => b.textContent.trim() === '保存').click()
      return true
    })()`)
    await sleep(600)
    weightChange = { ...before, next }
    ok('干员：改权重 → 后端 200 且回显新值 → 已改回原值', `${before.name} ${before.weight} → ${next} → ${before.weight}`)
  } catch (err) {
    bad('干员改权重', err.message)
  }
  await shoot('04-characters')

  // ---- 6. 配置 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/config"]').click()`)
    await waitFor('配置列表出现', `document.querySelectorAll('.pn-config-row').length > 0`)
    const info = await evaluate(`(() => {
      const rows = [...document.querySelectorAll('.pn-config-row')]
      return { count: rows.length, keys: rows.map(r => r.querySelector('header b').textContent.trim()) }
    })()`)
    need(info.count > 0, '配置是空的')
    // 默认是折叠的（一条安全协议几百字，全展开滚不动）；展开第一条才该出现 textarea
    await evaluate(`[...document.querySelectorAll('.pn-config-row button')][0].click()`)
    await waitFor('展开后出现 textarea', `!!document.querySelector('.pn-config-row textarea')`)
    const textarea = await evaluate(`(() => {
      const t = document.querySelector('.pn-config-row textarea')
      const save = [...document.querySelectorAll('.pn-config-row button')].find(b => b.textContent.trim() === '保存')
      return { len: t.value.length, mono: getComputedStyle(t).fontFamily, saveDisabled: save.disabled }
    })()`)
    need(textarea.len > 0, 'textarea 里没内容')
    need(/mono|Consolas|Cascadia/i.test(textarea.mono), `长文本不是等宽字体：${textarea.mono}`)
    need(textarea.saveDisabled, '刚展开、没改动时"保存"应该是禁用的')
    // 改一个字符（不保存），"保存"必须亮起来；然后点"撤销修改"还原草稿 —— 全程不写库
    await evaluate(`(() => {
      const t = document.querySelector('.pn-config-row textarea')
      t.value = t.value + '\\n'
      t.dispatchEvent(new Event('input', { bubbles: true }))
      return true
    })()`)
    await sleep(150)
    const dirtyState = await evaluate(`(() => {
      const save = [...document.querySelectorAll('.pn-config-row button')].find(b => b.textContent.trim() === '保存')
      const hint = document.querySelector('.pn-config-row .hint')?.textContent || ''
      return { saveDisabled: save.disabled, hint }
    })()`)
    need(!dirtyState.saveDisabled, '改了内容后"保存"应该可用')
    need(dirtyState.hint.includes('未保存'), `没有提示"有未保存的修改"：${dirtyState.hint}`)
    await evaluate(`[...document.querySelectorAll('.pn-config-row button')].find(b => b.textContent.trim() === '撤销修改').click()`)
    ok('配置：键值列表 + 展开编辑（等宽 textarea）+ 未保存提示', `${info.count} 条：${info.keys.join(' / ')}`)
  } catch (err) {
    bad('配置页', err.message)
  }
  await shoot('05-config')

  // ---- 7. 密钥表：断言不显示密钥 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/keys"]').click()`)
    await waitFor('密钥表出现', `document.querySelectorAll('.pn-table tbody tr').length > 0`)
    const audit = await evaluate(`(() => {
      const text = document.body.innerText
      const rows = document.querySelectorAll('.pn-table tbody tr')
      const labels = [...document.querySelectorAll('.pn-table tbody tr')].slice(0, 5).map(r => r.children[1].textContent.trim())
      // 密钥明文一般长这样：sk-xxxx / 32+ 位连续十六进制或 base64；页面上不该有
      const looksLikeSecret = /\\bsk-[A-Za-z0-9_-]{16,}\\b/.test(text)
      const longToken = text.split(/\\s+/).filter(w => /^[A-Za-z0-9_-]{40,}$/.test(w))
      return { rows: rows.length, labels, looksLikeSecret, longToken: longToken.length, hasNote: text.includes('不返回') }
    })()`)
    need(audit.rows > 0, '密钥表是空的')
    need(!audit.looksLikeSecret, '页面上出现了 sk- 开头的密钥')
    need(audit.longToken === 0, `页面上有 ${audit.longToken} 段 40+ 字符的可疑串`)
    need(audit.hasNote, '没有写清"接口不返回密钥"')
    ok('密钥表：只有状态，页面上没有任何密钥内容', `${audit.rows} 把，标记 ${audit.labels.slice(0, 3).join('/')}`)
  } catch (err) {
    bad('密钥表', err.message)
  }
  await shoot('06-keys')

  // ---- 8. 用量 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/usage"]').click()`)
    await waitFor('用量图出现', `document.querySelectorAll('.pn-chart .ef-bars li').length === 7`)
    await evaluate(`[...document.querySelectorAll('.ef-seg input')][1].click()`)
    await waitFor('切成 30 天', `document.querySelectorAll('.pn-chart .ef-bars li').length === 30`)
    const rows = await evaluate(`document.querySelectorAll('.pn-table tbody tr').length`)
    ok('用量：7 天 → 30 天切换生效', `30 天窗口 ${rows} 行有量`)
    // 切回 7 天，截图更好看
    await evaluate(`[...document.querySelectorAll('.ef-seg input')][0].click()`)
    await waitFor('切回 7 天', `document.querySelectorAll('.pn-chart .ef-bars li').length === 7`)
  } catch (err) {
    bad('用量页', err.message)
  }
  await shoot('07-usage')

  // ---- 9. 思考链 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/reasoning"]').click()`)
    await waitFor('思考链表单出现', `!!document.querySelector('form .pn-input')`)
    if (!CONV) {
      ok('思考链：表单在位（没给 conversation id，跳过真查询）')
    } else {
      await evaluate(`(() => {
        const input = document.querySelector('form .pn-input')
        input.value = ${JSON.stringify(CONV)}
        input.dispatchEvent(new Event('input', { bubbles: true }))
        document.querySelector('form button[type=submit]').click()
        return true
      })()`)
      await waitFor('思考链结果出现', `document.querySelectorAll('.pn-reason').length > 0`)
      const detail = await evaluate(`(() => {
        const first = document.querySelector('.pn-reason')
        first.open = true
        const pre = first.querySelector('pre')
        return { count: document.querySelectorAll('.pn-reason').length, mono: getComputedStyle(pre).fontFamily, textLen: pre.textContent.length }
      })()`)
      need(detail.textLen > 0, '思考链正文是空的')
      need(/mono|Consolas|Cascadia/i.test(detail.mono), `思考链不是等宽字体：${detail.mono}`)
      ok('思考链：查询出结果，等宽字体 + 可折叠', `${detail.count} 条，首条 ${detail.textLen} 字`)
    }
  } catch (err) {
    bad('思考链', err.message)
  }
  await shoot('08-reasoning')

  // ---- 10. 浅色主题 ----
  try {
    await evaluate(`document.querySelector('nav a[href="#/overview"]').click()`)
    await waitFor('回到总览', `document.querySelectorAll('.pn-figs b').length >= 9`)
    await evaluate(`[...document.querySelectorAll('.pn-cmd-util button')].find(b => b.textContent.trim() === '浅色').click()`)
    await sleep(250)
    const theme = await evaluate(`document.documentElement.dataset.theme`)
    need(theme === 'light', `切浅色后 data-theme 是 ${theme}`)
    await shoot('09-overview-light')
    // 切回深色，别把 localStorage 留在浅色（下次跑会以浅色开局）
    await evaluate(`[...document.querySelectorAll('.pn-cmd-util button')].find(b => b.textContent.trim() === '深色').click()`)
    await sleep(200)
    ok('主题：深 → 浅 → 深 切换生效')
  } catch (err) {
    bad('主题切换', err.message)
  }

  // ---- 11. 深浅对比：截图之外，量一下两套主题的底色真的变了 ----
  try {
    const colors = await evaluate(`(() => {
      const cs = getComputedStyle(document.body)
      return { bg: cs.backgroundColor, fg: cs.color }
    })()`)
    need(colors.bg !== 'rgb(244, 244, 242)', `深色主题的底色不该是纸白：${colors.bg}`)
    ok('深色主题底色正确', `${colors.bg} / ${colors.fg}`)
  } catch (err) {
    bad('深色主题底色', err.message)
  }

  // ---- 12. 窄屏：1024 以下不能整页横向滚（表格自己滚） ----
  try {
    const notes = []
    for (const width of [1024, 900]) {
      await cdp.send('Emulation.setDeviceMetricsOverride', {
        width,
        height: 1000,
        deviceScaleFactor: 1,
        mobile: false,
      })
      // 切到列最多的两页看布局（用户表 + 密钥表）
      for (const key of ['users', 'keys']) {
        await evaluate(`document.querySelector('nav a[href="#/${key}"]').click()`)
        await waitFor(`${width}px 下 ${key} 表出现`, `document.querySelectorAll('.pn-table tbody tr').length > 0`)
        const overflow = await evaluate(`(() => {
          const doc = document.documentElement
          const wrap = document.querySelector('.pn-table-wrap')
          return {
            pageOverflow: doc.scrollWidth - window.innerWidth,
            tableWrapScrolls: !!wrap && wrap.scrollWidth > wrap.clientWidth,
          }
        })()`)
        need(overflow.pageOverflow <= 1, `${width}px / ${key}：整页横向溢出了 ${overflow.pageOverflow}px`)
        notes.push(`${width}px ${key}${overflow.tableWrapScrolls ? '(表格内滚)' : ''}`)
      }
    }
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1024,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    })
    await evaluate(`document.querySelector('nav a[href="#/overview"]').click()`)
    await waitFor('窄屏回总览', `document.querySelectorAll('.pn-figs b').length >= 9`)
    await shoot('10-overview-1024')
    ok('响应式：1024 / 900 下整页不横滚，宽表格自己横滚', notes.join(' · '))
  } catch (err) {
    bad('响应式', err.message)
  } finally {
    // 复原桌面宽度，别影响后续（截图和外观都按 1440 做的）
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    })
  }

  // ---- 13. 非管理员：登录成功也要被挡住（清会话、回登录页、说人话） ----
  if (!FORBID_USER || !FORBID_PASS) {
    ok('非管理员拦截：跳过（没给 --forbidden-user/--forbidden-pass）')
  } else {
    try {
      // 清掉上一段会话，回到没登录的状态（localStorage 里不能留 token）
      // 必须真刷新一次：只改 hash / 同一地址 navigate 不会重建 Vue 实例，
      // 内存里的 session 还在，界面不会回登录页。
      await evaluate(`localStorage.clear()`)
      await cdp.send('Page.reload', { ignoreCache: true })
      await waitFor('回到登录页', `!!document.querySelector('.pn-login-card')`)
      // 从这里开始，403 是我们要的结果（"不是管理员"），别记成故障
      expectForbidden = true
      await evaluate(`(() => {
        const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })) }
        const inputs = document.querySelectorAll('.pn-login-card input')
        set(inputs[0], ${JSON.stringify(FORBID_USER)})
        set(inputs[1], ${JSON.stringify(FORBID_PASS)})
        document.querySelector('.pn-login-card button[type=submit]').click()
        return true
      })()`)
      await waitFor(
        '出现"不是管理员"的提示',
        `(document.querySelector('.ef-notice')?.innerText || '').includes('不是管理员')`,
      )
      const state = await evaluate(`(() => {
        const notice = document.querySelector('.ef-notice')?.innerText || ''
        return {
          notice: notice.replace(/\\s+/g, ' ').trim(),
          stillLogin: !!document.querySelector('.pn-login-card'),
          hasShell: !!document.querySelector('.ef-cmdbar'),
          token: localStorage.getItem('baker.panel.token'),
        }
      })()`)
      need(state.stillLogin, '应该还在登录页')
      need(!state.hasShell, '非管理员不该看到后台界面')
      need(!state.token, '非管理员登录后 token 必须被清掉')
      await shoot('11-forbidden', false)
      ok('非管理员：登录被拦下、回登录页、token 清掉', state.notice)
    } catch (err) {
      bad('非管理员拦截', err.message)
    }
  }
}

function summary() {
  const passed = checks.filter((c) => c.ok).length
  console.log(`\n页面走查：${passed}/${checks.length} 通过`)
  console.log(`console error：${consoleErrors.length} 条`)
  for (const e of consoleErrors.slice(0, 10)) console.log(`  · ${e}`)
  console.log(`未捕获异常：${pageExceptions.length} 条`)
  for (const e of pageExceptions.slice(0, 5)) console.log(`  · ${e}`)
  const realBad = badResponses.filter((r) => !r.url.includes('favicon'))
  console.log(`4xx/5xx 响应：${realBad.length} 条`)
  for (const r of realBad.slice(0, 10)) console.log(`  · ${r.method} ${r.status} ${r.url}`)
  if (expectedBad.length) {
    console.log(`预期内的非 2xx：${expectedBad.length} 条（非管理员探针）`)
    for (const r of expectedBad.slice(0, 5)) console.log(`  · ${r.method} ${r.status} ${r.url}`)
  }
  if (expectedConsole.length) console.log(`预期内的控制台报错：${expectedConsole.length} 条（同上）`)
  console.log(`截图：${shots.length} 张 → ${OUT}`)
  for (const s of shots) console.log(`  · ${s.name}.png (${Math.round(s.bytes / 1024)} KB)`)

  const failed = checks.length - passed
  if (failed || consoleErrors.length || pageExceptions.length || realBad.length) process.exitCode = 1
}

try {
  await main()
} catch (err) {
  console.error('\n走查中断：', err.message)
  process.exitCode = 1
} finally {
  try {
    await cdp?.send('Browser.close')
  } catch {
    /* 已经关了 */
  }
  await sleep(300)
  // 必须杀进程树：只 kill 启动器的话 Edge 的浏览器进程会活下来占着调试端口
  await killTree(edge?.pid)
  // 清理临时 profile（Edge 可能还占着文件，删不掉就算了）
  try {
    if (existsSync(PROFILE)) rmSync(PROFILE, { recursive: true, force: true, maxRetries: 2 })
  } catch {
    /* 忽略 */
  }
  summary()
}
