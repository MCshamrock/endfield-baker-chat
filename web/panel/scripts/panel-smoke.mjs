#!/usr/bin/env node
/**
 * scripts/panel-smoke.mjs —— 面板的接口层冒烟（不需要真后端、不连数据库）。
 *
 * 为什么要有这个脚本：
 *   真后端联调很贵（要账号、要库、要 5660 在跑），但接口层会出问题的地方其实很集中：
 *   token 有没有带上、401 有没有分流、错误体有没有被读懂、分页参数有没有真的发出去、
 *   PATCH 之后回显对不对。这些用一台假后端就能全部覆盖，而且随时能跑。
 *
 * 假后端刻意"像"真的：
 *   · 错误体形状照 `web/back/app/errors.py`：`{"error": {"code", "message"}}`；
 *   · 登录响应里**没有 role**（真后端也没有）—— 所以"是不是管理员"只能靠打后台接口试；
 *   · `days[]` 只回有请求的那两天（真后端就是这么干的），前端要自己补零；
 *   · 干员接口故意回重复行（真后端现在的 join 就有这个毛病），看前端去不去重。
 *
 * 跑法：
 *   node scripts/panel-smoke.mjs        （Node ≥ 22.18 可以直接 import .ts）
 * 退出码：0 = 全绿，1 = 有失败。
 */

import { createServer } from 'node:http'
import { createApiClient, ApiError, memoryTokenStore } from '../src/lib/api.ts'

// ---------------------------------------------------------------- 断言小工具
const results = []

async function test(name, fn) {
  try {
    await fn()
    results.push({ name, ok: true, detail: '' })
    console.log(`PASS  ${name}`)
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    results.push({ name, ok: false, detail })
    console.log(`FAIL  ${name}\n      ${detail}`)
  }
}

function assert(cond, message) {
  if (!cond) throw new Error(message)
}

function assertEqual(actual, expected, what) {
  if (actual !== expected) {
    throw new Error(`${what}：期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`)
  }
}

// ---------------------------------------------------------------- 假后端的库
const USERS = [
  { public_id: 'u-admin', login_id: 'panel_admin', pass: 'AdminPass123', role: 'admin', status: 'active', display_name: '测试管理员', devices: 1, created_at: '2026-11-17T10:00:00+00:00', last_seen_at: '2026-11-17T17:30:00+00:00' },
  { public_id: 'u-plain', login_id: 'normal_user', pass: 'UserPass123', role: 'user', status: 'active', display_name: '普通用户', devices: 2, created_at: '2026-11-16T10:00:00+00:00', last_seen_at: null },
  { public_id: 'u-off', login_id: 'disabled_user', pass: 'OffPass123', role: 'user', status: 'disabled', display_name: '停用用户', devices: 0, created_at: '2026-11-15T10:00:00+00:00', last_seen_at: '2026-11-16T09:00:00+00:00' },
  { public_id: 'u-zoe', login_id: 'zoe', pass: 'ZoePass123', role: 'user', status: 'active', display_name: '佐伊', devices: 1, created_at: '2026-11-14T10:00:00+00:00', last_seen_at: '2026-11-17T16:00:00+00:00' },
]

const CHARACTERS = [
  { id: 'perlica', name: '佩丽卡', avatar: '', attribute: '终末地工业', enabled: 1, active_weight: 57, sort_order: 3, prompt_ver: 1 },
  { id: 'yifeng', name: '伊冯', avatar: '', attribute: '', enabled: 1, active_weight: 46, sort_order: 1, prompt_ver: 2 },
  { id: 'chen', name: '陈千语', avatar: '', attribute: '', enabled: 0, active_weight: 12, sort_order: 9, prompt_ver: 1 },
]

const CONFIG = [
  { key: 'safety_protocol', value: '【身份与边界】\n1. 你就是当前角色。', updated_at: '2026-11-15T12:13:51+00:00' },
  { key: 'worldview', value: '你是一个基于终末地世界观的角色。', updated_at: '2026-11-15T12:13:51+00:00' },
]

const KEYS = [
  { id: 1, label: 'k00', enabled: true, cooling: false, cooldown_until: null, fail_count: 0, rpm: 10, base_url: 'https://api.example.com/v1/', model: 'agnes-2.5-flash', last_used_at: '2026-11-17T17:28:00+00:00' },
  { id: 2, label: 'k01', enabled: true, cooling: true, cooldown_until: '2026-11-17T18:00:00+00:00', fail_count: 3, rpm: 10, base_url: 'https://api.example.com/v1/', model: 'agnes-2.5-flash', last_used_at: '2026-11-17T17:10:00+00:00' },
  { id: 3, label: 'k02', enabled: false, cooling: false, cooldown_until: null, fail_count: 0, rpm: 10, base_url: 'https://api.example.com/v1/', model: 'agnes-2.5-flash', last_used_at: null },
]

/** 设备 token → 绑定的账号 public_id（null = 没登录的访客） */
const devices = new Map()
/** 每次请求都记一笔，用来断言"参数真的发出去了" */
const log = []
let deviceSeq = 0
/** 模拟"反代挂了"：把 overview 换成 502 + HTML，用来验证前端读不懂 JSON 时的兜底文案 */
let breakOverview = false

// ---------------------------------------------------------------- 假后端本体
function json(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  res.end(payload)
}

function fail(res, status, code, message) {
  json(res, status, { error: { code, message } })
}

/** 从 Authorization 里认出设备；认不出返回 null */
function deviceOf(req) {
  const header = req.headers.authorization || ''
  if (!header.startsWith('Bearer ')) return { status: 401, code: 'no_token', message: '缺少 Authorization: Bearer <token>' }
  const token = header.slice(7).trim()
  if (!devices.has(token)) return { status: 401, code: 'bad_token', message: 'token 无效或已被停用' }
  return { token, owner: devices.get(token) }
}

/** 管理员才放行 */
function adminOf(req, res) {
  const device = deviceOf(req)
  if (device.code) {
    fail(res, device.status, device.code, device.message)
    return null
  }
  const user = USERS.find((u) => u.public_id === device.owner)
  if (!user || user.role !== 'admin' || user.status !== 'active') {
    fail(res, 403, 'forbidden', '需要管理员权限')
    return null
  }
  return user
}

function toUserOut(u) {
  return {
    id: u.public_id,
    username: u.login_id,
    display_name: u.display_name,
    status: u.status,
    role: u.role,
    devices: u.devices,
    created_at: u.created_at,
    last_seen_at: u.last_seen_at,
  }
}

const DAYS = [
  { date: '2026-11-15', requests: 30, prompt_tokens: 158522, completion_tokens: 1292, cached_tokens: 59904, avg_ms: 6276 },
  { date: '2026-11-17', requests: 5, prompt_tokens: 38630, completion_tokens: 1124, cached_tokens: 18432, avg_ms: 2916 },
]

function overviewBody() {
  const enabled = KEYS.filter((k) => k.enabled).length
  return {
    counts: { users: USERS.length, devices: 81, conversations: 73, messages: 163, characters: CHARACTERS.length, worldbook: 147 },
    keys: { total: KEYS.length, enabled, cooldown: KEYS.filter((k) => k.enabled && k.cooling).length, disabled: KEYS.length - enabled },
    today: { requests: 5, prompt_tokens: 38630, completion_tokens: 1124, cached_tokens: 18432, avg_ms: 2916 },
    days: DAYS,
    now: '2026-11-17T17:40:00+00:00',
  }
}

async function readBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  if (!chunks.length) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    return null
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  log.push({ method: req.method, pathname: url.pathname, query: Object.fromEntries(url.searchParams) })

  // ---- 设备 / 登录 ----
  if (req.method === 'POST' && url.pathname === '/v1/auth/device') {
    deviceSeq += 1
    const token = `dev-token-${deviceSeq}`
    devices.set(token, null)
    json(res, 201, { device_id: `device-${deviceSeq}`, token })
    return
  }

  if (req.method === 'POST' && url.pathname === '/v1/auth/login') {
    const device = deviceOf(req)
    if (device.code) return fail(res, device.status, device.code, device.message)
    const body = await readBody(req)
    const user = USERS.find((u) => u.login_id === body?.username)
    if (!user || user.pass !== body?.password) return fail(res, 401, 'bad_credentials', '用户名或密码不对')
    if (user.status !== 'active') return fail(res, 403, 'account_disabled', '这个账号已被停用')
    devices.set(device.token, user.public_id)
    // 真后端这里**没有 role**：故意保持一样，前端不能靠它判断权限
    json(res, 200, {
      user: { id: user.public_id, username: user.login_id, display_name: user.display_name, created_at: user.created_at },
      adopted_conversations: 0,
    })
    return
  }

  if (req.method === 'POST' && url.pathname === '/v1/auth/logout') {
    const device = deviceOf(req)
    if (device.code) return fail(res, device.status, device.code, device.message)
    devices.set(device.token, null)
    res.writeHead(204)
    res.end()
    return
  }

  if (url.pathname === '/v1/auth/me') {
    const device = deviceOf(req)
    if (device.code) return fail(res, device.status, device.code, device.message)
    const user = USERS.find((u) => u.public_id === device.owner)
    if (!user) return json(res, 200, { kind: 'guest' })
    return json(res, 200, { kind: 'user', user: { id: user.public_id, username: user.login_id, display_name: user.display_name, created_at: user.created_at }, devices: [] })
  }

  // ---- 后台 ----
  if (url.pathname === '/__break-overview') {
    breakOverview = true
    res.writeHead(204)
    res.end()
    return
  }
  if (url.pathname === '/__fix-overview') {
    breakOverview = false
    res.writeHead(204)
    res.end()
    return
  }

  if (url.pathname === '/v1/admin/overview' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    if (breakOverview) {
      res.writeHead(502, { 'Content-Type': 'text/html' })
      res.end('<html><body><h1>502 Bad Gateway</h1></body></html>')
      return
    }
    return json(res, 200, overviewBody())
  }

  if (url.pathname === '/v1/admin/users' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    const q = (url.searchParams.get('q') || '').trim()
    const limit = Math.max(1, Math.min(Number(url.searchParams.get('limit') || 50), 200))
    const offset = Math.max(0, Number(url.searchParams.get('offset') || 0))
    const hit = USERS.filter((u) => !q || u.login_id.includes(q) || u.display_name.includes(q))
    return json(res, 200, { total: hit.length, limit, offset, items: hit.slice(offset, offset + limit).map(toUserOut) })
  }

  const userPatch = url.pathname.match(/^\/v1\/admin\/users\/([^/]+)$/)
  if (userPatch && req.method === 'PATCH') {
    const admin = adminOf(req, res)
    if (!admin) return
    const target = USERS.find((u) => u.public_id === decodeURIComponent(userPatch[1]))
    if (!target) return fail(res, 404, 'no_user', '没有这个账号')
    const body = (await readBody(req)) || {}
    if (body.role !== undefined && target.public_id === admin.public_id && body.role !== 'admin') {
      return fail(res, 400, 'cannot_demote_self', '不能取消自己的管理员权限')
    }
    if (body.status !== undefined) target.status = body.status
    if (body.role !== undefined) target.role = body.role
    if (body.display_name !== undefined) target.display_name = String(body.display_name).trim()
    return json(res, 200, {
      id: target.public_id,
      username: target.login_id,
      display_name: target.display_name,
      status: target.status,
      role: target.role,
    })
  }

  if (url.pathname === '/v1/admin/characters' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    // 故意把每行重复一次：真后端 join character_prompts 就是 36 → 72 行
    return json(res, 200, { items: CHARACTERS.flatMap((c) => [{ ...c }, { ...c }]) })
  }

  const charPatch = url.pathname.match(/^\/v1\/admin\/characters\/([^/]+)$/)
  if (charPatch && req.method === 'PATCH') {
    if (!adminOf(req, res)) return
    const target = CHARACTERS.find((c) => c.id === decodeURIComponent(charPatch[1]))
    if (!target) return fail(res, 404, 'no_character', '没有这个角色，或者它已下架')
    const body = (await readBody(req)) || {}
    if (body.enabled !== undefined) target.enabled = body.enabled ? 1 : 0
    if (body.active_weight !== undefined) target.active_weight = body.active_weight
    if (body.sort_order !== undefined) target.sort_order = body.sort_order
    if (body.name !== undefined) target.name = body.name
    return json(res, 200, {
      id: target.id,
      name: target.name,
      enabled: Boolean(target.enabled),
      active_weight: target.active_weight,
      sort_order: target.sort_order,
    })
  }

  if (url.pathname === '/v1/admin/config' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    return json(res, 200, { items: CONFIG })
  }

  const configPut = url.pathname.match(/^\/v1\/admin\/config\/([^/]+)$/)
  if (configPut && req.method === 'PUT') {
    if (!adminOf(req, res)) return
    const key = decodeURIComponent(configPut[1])
    const body = (await readBody(req)) || {}
    const entry = CONFIG.find((c) => c.key === key)
    const updated = { key, value: String(body.value ?? ''), updated_at: '2026-11-17T17:45:00+00:00' }
    if (entry) Object.assign(entry, updated)
    else CONFIG.push(updated)
    return json(res, 200, updated)
  }

  if (url.pathname === '/v1/admin/keys' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    return json(res, 200, { items: KEYS })
  }

  const keyPatch = url.pathname.match(/^\/v1\/admin\/keys\/(\d+)$/)
  if (keyPatch && req.method === 'PATCH') {
    if (!adminOf(req, res)) return
    const target = KEYS.find((k) => k.id === Number(keyPatch[1]))
    if (!target) return fail(res, 404, 'no_key', '没有这把密钥')
    const body = (await readBody(req)) || {}
    if (body.enabled !== undefined) target.enabled = Boolean(body.enabled)
    return json(res, 200, { id: target.id, label: target.label, enabled: target.enabled })
  }

  if (url.pathname === '/v1/admin/usage' && req.method === 'GET') {
    if (!adminOf(req, res)) return
    return json(res, 200, { days: Number(url.searchParams.get('days') || 7), items: DAYS })
  }

  const reason = url.pathname.match(/^\/v1\/admin\/conversations\/([^/]+)\/reasoning$/)
  if (reason && req.method === 'GET') {
    if (!adminOf(req, res)) return
    const id = decodeURIComponent(reason[1])
    if (id !== 'conv-abc') return fail(res, 404, 'no_conversation', '会话不存在，或者不属于这台设备')
    return json(res, 200, {
      conversation_id: id,
      items: [
        { seq: 12, speaker: 'ai', reply: '嗯，我在。', reasoning: '她在问我在不在。先回一句短的，别抢话。', created_at: '2026-11-17T17:30:00+00:00' },
      ],
    })
  }

  fail(res, 404, 'not_found', '这个地址不存在')
})

// ---------------------------------------------------------------- 跑测试
async function main() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  const baseUrl = `http://127.0.0.1:${port}`

  let unauthorizedHits = 0
  const tokenStore = memoryTokenStore()
  const client = createApiClient({
    baseUrl,
    tokenStore,
    onUnauthorized: () => {
      unauthorizedHits += 1
    },
  })

  // 假的"别人"：跟被测客户端共用一台假后端，但 token 独立
  const plainStore = memoryTokenStore()
  const plainClient = createApiClient({ baseUrl, tokenStore: plainStore })

  console.log(`假后端跑在 ${baseUrl}\n`)

  await test('设备注册：拿到 token 并写进 tokenStore', async () => {
    const token = await client.ensureDevice()
    assert(/^dev-token-\d+$/.test(token), `token 形状不对：${token}`)
    assertEqual(tokenStore.get(), token, 'tokenStore 里的 token')
    const body = log.findLast((r) => r.pathname === '/v1/auth/device')
    assert(body, '没有发出注册设备的请求')
    assertEqual(body.method, 'POST', '注册设备的方法')
  })

  await test('登录失败：401 + bad_credentials + 中文话，且不触发"回登录页"', async () => {
    const before = unauthorizedHits
    let caught = null
    try {
      await client.login('panel_admin', 'wrong-password')
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 401, '状态码')
    assertEqual(caught.code, 'bad_credentials', '错误码')
    assertEqual(caught.message, '用户名或密码不对', '错误文案')
    assertEqual(unauthorizedHits, before, '登录失败的 401 不应该触发回登录页')
  })

  await test('登录成功：返回 user，且响应里没有 role（权限要另探）', async () => {
    const user = await client.login('panel_admin', 'AdminPass123')
    assertEqual(user.username, 'panel_admin', '用户名')
    assertEqual(user.display_name, '测试管理员', '显示名')
    assertEqual(user.role, undefined, 'user.role 不该存在')
  })

  let overview
  await test('overview：首页要用的字段一个不少', async () => {
    overview = await client.overview()
    for (const field of ['users', 'devices', 'conversations', 'messages', 'characters']) {
      assert(typeof overview.counts[field] === 'number', `counts.${field} 缺失`)
    }
    for (const field of ['total', 'enabled', 'cooldown', 'disabled']) {
      assert(typeof overview.keys[field] === 'number', `keys.${field} 缺失`)
    }
    for (const field of ['requests', 'prompt_tokens', 'completion_tokens', 'cached_tokens', 'avg_ms']) {
      assert(typeof overview.today[field] === 'number', `today.${field} 缺失`)
    }
    assert(Array.isArray(overview.days), 'days 不是数组')
    assert(overview.days.length === 2, `假后端只给 2 天，实际 ${overview.days.length}`)
    assert(/^\d{4}-\d{2}-\d{2}$/.test(overview.days[0].date), `days[].date 格式不对：${overview.days[0].date}`)
    assert(!Number.isNaN(Date.parse(overview.now)), `now 不是合法 ISO：${overview.now}`)
  })

  await test('分页：q / limit / offset 真的发到服务端，回包也带着它们', async () => {
    const page = await client.listUsers({ q: 'user', limit: 1, offset: 1 })
    const sent = log.findLast((r) => r.pathname === '/v1/admin/users' && r.query.q === 'user')
    assert(sent, '服务端没收到带 q 的请求')
    assertEqual(sent.query.limit, '1', '发出去的 limit')
    assertEqual(sent.query.offset, '1', '发出去的 offset')
    assertEqual(page.limit, 1, '回包的 limit')
    assertEqual(page.offset, 1, '回包的 offset')
    assertEqual(page.total, 2, '命中数（normal_user / disabled_user）')
    assertEqual(page.items.length, 1, '这一页的行数')
  })

  await test('分页：空 q 不带参数（避免 q= 这类空搜索）', async () => {
    await client.listUsers({ limit: 20, offset: 0 })
    const sent = log.findLast((r) => r.pathname === '/v1/admin/users')
    assert(!('q' in sent.query), `空 q 不该出现在查询串里：${JSON.stringify(sent.query)}`)
  })

  await test('用户 PATCH：改角色/状态/显示名，再查回来是新的', async () => {
    const patched = await client.patchUser('u-zoe', { role: 'admin', display_name: '佐伊（改）' })
    assertEqual(patched.role, 'admin', 'PATCH 回包的 role')
    assertEqual(patched.display_name, '佐伊（改）', 'PATCH 回包的 display_name')
    const page = await client.listUsers({ q: 'zoe' })
    const row = page.items[0]
    assertEqual(row.role, 'admin', '重新查回来的 role')
    assertEqual(row.display_name, '佐伊（改）', '重新查回来的 display_name')
    const off = await client.patchUser('u-off', { status: 'active' })
    assertEqual(off.status, 'active', '改状态')
    await client.patchUser('u-off', { status: 'disabled' }) // 复原，别影响后面的断言
  })

  await test('不能把自己降权：后端 400 cannot_demote_self，前端拿到的是人话', async () => {
    let caught = null
    try {
      await client.patchUser('u-admin', { role: 'user' })
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 400, '状态码')
    assertEqual(caught.code, 'cannot_demote_self', '错误码')
    assertEqual(caught.message, '不能取消自己的管理员权限', '错误文案')
  })

  await test('普通账号：登录成功，但打后台接口是 403（不是 401，不该被踢回登录页）', async () => {
    const before = unauthorizedHits
    await plainClient.ensureDevice()
    const user = await plainClient.login('normal_user', 'UserPass123')
    assertEqual(user.username, 'normal_user', '普通账号用户名')
    let caught = null
    try {
      await plainClient.overview()
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 403, '状态码')
    assertEqual(caught.code, 'forbidden', '错误码')
    assert(caught.isForbidden, 'isForbidden 应为 true')
    assert(!caught.isUnauthorized, '403 不该被当成 401')
    assertEqual(unauthorizedHits, before, '403 不应该触发回登录页')
  })

  await test('401：坏 token 打后台 → ApiError 401 且触发一次"回登录页"', async () => {
    const before = unauthorizedHits
    const badStore = memoryTokenStore('dev-token-does-not-exist')
    let hits = 0
    const badClient = createApiClient({ baseUrl, tokenStore: badStore, onUnauthorized: () => { hits += 1 } })
    let caught = null
    try {
      await badClient.overview()
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 401, '状态码')
    assertEqual(caught.code, 'bad_token', '错误码')
    assertEqual(hits, 1, 'onUnauthorized 的触发次数')
    assertEqual(unauthorizedHits, before, '别的客户端的 401 不该影响这个客户端')
  })

  await test('缺 token：401 + no_token', async () => {
    const emptyClient = createApiClient({ baseUrl, tokenStore: memoryTokenStore(null) })
    let caught = null
    try {
      await emptyClient.listKeys()
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 401, '状态码')
    assertEqual(caught.code, 'no_token', '错误码')
  })

  await test('干员：权重/排序/上下架 PATCH 后回显', async () => {
    const list = await client.listCharacters()
    assertEqual(list.items.length, 6, '假后端故意回 6 行（3 个干员 × 2）')
    const out = await client.patchCharacter('perlica', { active_weight: 66, sort_order: 2 })
    assertEqual(out.active_weight, 66, '权重回显')
    assertEqual(out.sort_order, 2, '排序回显')
    const off = await client.patchCharacter('chen', { enabled: true })
    assertEqual(off.enabled, true, '上架回显')
    await client.patchCharacter('chen', { enabled: false })
  })

  await test('干员：打不存在的 slug → 404 + 中文话（错误不会被吞成白屏）', async () => {
    let caught = null
    try {
      await client.patchCharacter('nobody', { active_weight: 50 })
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 404, '状态码')
    assertEqual(caught.code, 'no_character', '错误码')
  })

  await test('配置：PUT 后回显新值与新的 updated_at', async () => {
    const before = (await client.listConfig()).items.find((i) => i.key === 'safety_protocol')
    assert(before, '假后端里没有 safety_protocol')
    const out = await client.putConfig('safety_protocol', '【身份与边界】\n改过了。')
    assertEqual(out.key, 'safety_protocol', 'key')
    assertEqual(out.value, '【身份与边界】\n改过了。', 'value 原样回来（含换行）')
    assert(out.updated_at !== before.updated_at, 'updated_at 应该变新')
    const after = (await client.listConfig()).items.find((i) => i.key === 'safety_protocol')
    assertEqual(after.value, '【身份与边界】\n改过了。', '重新查回来是新的')
  })

  await test('配置：key 里的斜杠/空格要编码，不能被当成路径', async () => {
    const out = await client.putConfig('weird key/with slash', 'v')
    assertEqual(out.key, 'weird key/with slash', '编码后仍能正确落库')
    const sent = log.findLast((r) => r.pathname.startsWith('/v1/admin/config/') && r.method === 'PUT')
    assert(!sent.pathname.includes('with slash'), `路径没编码：${sent.pathname}`)
  })

  await test('密钥：只回状态，任何"密钥内容"字段都不存在', async () => {
    const keys = (await client.listKeys()).items
    assertEqual(keys.length, 3, '假后端 3 把 key')
    const allowed = new Set(['id', 'label', 'enabled', 'cooling', 'cooldown_until', 'fail_count', 'rpm', 'base_url', 'model', 'last_used_at'])
    for (const key of keys) {
      for (const field of Object.keys(key)) {
        assert(allowed.has(field), `出现了不该有的字段：${field}`)
        assert(!/key|secret|token|enc/i.test(field), `字段名像密钥：${field}`)
      }
    }
    assert(keys.some((k) => k.cooling), '假后端应该有一把冷却中的 key（用来验证状态显示）')
  })

  await test('密钥：启停 PATCH 后回显', async () => {
    const out = await client.patchKey(3, true)
    assertEqual(out.enabled, true, '启用回显')
    assertEqual(out.label, 'k02', 'label 回显')
    await client.patchKey(3, false)
  })

  await test('用量：days 参数生效（7/30/90 各打一次）', async () => {
    for (const days of [7, 30, 90]) {
      const out = await client.usage(days)
      assertEqual(out.days, days, `回包的 days（请求 ${days}）`)
    }
    const sent = log.filter((r) => r.pathname === '/v1/admin/usage').map((r) => r.query.days)
    assertEqual(sent.join(','), '7,30,90', '发出去的三次 days')
  })

  await test('思考链：正常会话回 items，未知会话 404', async () => {
    const ok = await client.reasoning('conv-abc', 20)
    assertEqual(ok.conversation_id, 'conv-abc', '会话 id')
    assertEqual(ok.items.length, 1, '条数')
    assert(ok.items[0].reasoning.length > 0, 'reasoning 不该是空的')
    let caught = null
    try {
      await client.reasoning('conv-nope')
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 404, '状态码')
    assertEqual(caught.code, 'no_conversation', '错误码')
  })

  await test('退出：204 不炸，token 被清掉', async () => {
    const store = memoryTokenStore()
    const c = createApiClient({ baseUrl, tokenStore: store })
    await c.ensureDevice()
    await c.login('panel_admin', 'AdminPass123')
    assert(store.get(), '退出前应该有 token')
    await c.logout()
    assertEqual(store.get(), null, '退出后 token 应该没了')
  })

  await test('错误体不是 JSON（反代 502 HTML）→ 有中文兜底，不是 "Unexpected token"', async () => {
    // 先把 overview 切成"反代挂了"模式：回 502 + HTML（真实场景里 nginx 502 就是这样）
    await fetch(`${baseUrl}/__break-overview`)
    let caught = null
    try {
      await client.overview()
    } catch (err) {
      caught = err
    }
    await fetch(`${baseUrl}/__fix-overview`)
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 502, '状态码')
    assertEqual(caught.message, '后端出错了，稍后再试', '502 的兜底文案')
    // 恢复之后能正常用（别把假后端留在坏状态里）
    const ok = await client.overview()
    assertEqual(ok.counts.characters, 3, '恢复后的 overview')
  })

  await test('连不上后端 → status 0 + 能动手的中文话（不是 NetworkError）', async () => {
    const dead = createApiClient({ baseUrl: 'http://127.0.0.1:1', tokenStore: memoryTokenStore('x') })
    let caught = null
    try {
      await dead.overview()
    } catch (err) {
      caught = err
    }
    assert(caught instanceof ApiError, '没有抛出 ApiError')
    assertEqual(caught.status, 0, '状态码')
    assertEqual(caught.code, 'network_error', '错误码')
    assert(caught.message.includes('连不上后端'), `文案应能动手：${caught.message}`)
  })

  await test('overview 全量数字与假后端一致（防止前端自己加减算错）', async () => {
    const data = await client.overview()
    assertEqual(data.keys.total, 3, 'keys.total')
    assertEqual(data.keys.enabled, 2, 'keys.enabled')
    assertEqual(data.keys.cooldown, 1, 'keys.cooldown')
    assertEqual(data.keys.disabled, 1, 'keys.disabled')
    assertEqual(data.counts.characters, 3, 'counts.characters')
  })

  server.close()

  // ---- 汇总 ----
  const passed = results.filter((r) => r.ok).length
  console.log(`\n${passed}/${results.length} 通过`)
  if (passed !== results.length) {
    console.log('\n失败的：')
    for (const r of results.filter((x) => !x.ok)) console.log(`  · ${r.name} —— ${r.detail}`)
    process.exitCode = 1
  }
}

main().catch((err) => {
  console.error('冒烟脚本自己挂了：', err)
  server.close()
  process.exitCode = 1
})
