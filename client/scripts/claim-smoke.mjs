#!/usr/bin/env node
// =============================================================================
// claim-smoke.mjs —— 取件码（上传 / 验码 / 领取 / 注册带码）冒烟测试
// -----------------------------------------------------------------------------
// 目的：**不启动界面、也不等真后端**，用一个假后端把前端真正会发的请求跑一遍。
//   · 假后端：Node 内置 http，监听随机端口，实现契约里的 4 个端点
//     （外加 /v1/auth/device、/v1/imports，因为领取要用设备 token、注册带码要读导入记录）
//   · 前端侧：不是手写 fetch —— 而是把 src/utils/cloudData.ts 与 src/utils/auth.ts
//     用 esbuild 打成临时模块后**直接调用**，所以测的是界面里真正跑的那段代码
//     （请求体 / FormData 字段名 / 错误文案 / 状态码映射都在里面）
//
// 为什么用 esbuild 现打一个包：这两个 .ts 里用了 `import.meta.env` 与浏览器全局
// （localStorage），Node 直接 import 会解析不了；打包 + 打两个 define + 一个
// localStorage 内存实现，就能在 Node 里原样跑。
//
// 用法：
//   node scripts/claim-smoke.mjs                 # 假后端（默认，不需要真后端）
//   node scripts/claim-smoke.mjs --live          # 真实后端，默认 http://127.0.0.1:5660
//   BAKER_BACKEND=http://<ip>:<port> node scripts/claim-smoke.mjs   # 也是真实后端，指定地址
//
// 真实后端模式只跑"一定会发生"的路径（上传 / 验码 / 领取 / 注册带码 / 坏码回滚）；
// 413、507、429、410 需要制造事故才能在真后端上触发（塞 64MB、灌满磁盘、连传 6 次、
// 等 24 小时），这些分支在假后端模式里已经覆盖。
// =============================================================================

import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
// esbuild 是 CJS 包，用默认导入最稳（命名导出在部分 Node 版本上取不到）
import esbuild from 'esbuild'

const HERE = path.dirname(fileURLToPath(import.meta.url))

let passed = 0
let failed = 0

function ok(name, detail = '') {
  passed += 1
  console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`)
}
function bad(name, detail = '') {
  failed += 1
  console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`)
}
function assert(cond, name, detail = '') {
  if (cond) ok(name, detail)
  else bad(name, detail)
  return !!cond
}
/** 断言"抛出来的错误文案里包含某几个关键词" */
async function expectThrows(fn, name, keywords = []) {
  try {
    await fn()
    bad(name, '没有抛错（预期失败）')
    return ''
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    const missing = keywords.filter((k) => !message.includes(k))
    if (missing.length) bad(name, `文案缺少 ${missing.join('/')}；实际：${message}`)
    else ok(name, message.slice(0, 46) + (message.length > 46 ? '…' : ''))
    return message
  }
}

// =============================================================================
// 1. 假后端
// =============================================================================

const DEVICE_TOKEN = 'smoke-device-token'
const EXPIRED_CODE = 'EXPIRE01'
const UNKNOWN_CODE = 'NOSUCH99'
const BAD_CLAIM_CODE = 'BADCODE1'

/** 已上传的码：code → { bytes, source, expiresAt, claimedBy:Set, claimCount } */
const codes = new Map()
/** 上一个上传请求被解析出的文件信息（用来核对 FormData 字段名与文件名） */
let lastUpload = null
/** 上传次数（限流测试用，契约：同一 IP 每小时 5 次） */
let uploadCount = 0
/** 强制 claim 回 401（模拟"设备 token 失效/没登录"） */
let forceClaimUnauthorized = false
/** 最近一次导入结果（/v1/imports 用） */
let latestImport = null
/** 假后端自己的导入结果模板 */
function importPayload({ id, duplicate = false, source = 'json' } = {}) {
  return {
    id: id ?? `imp_${Date.now().toString(36)}`,
    duplicate,
    source,
    mode: 'merge',
    package_version: 1,
    cards: 1,
    conversations: 2,
    messages: 37,
    images: 3,
    skipped: 1,
    notes: ['未收录角色「测试角色」已跳过'],
    created_at: new Date().toISOString(),
    revoked_at: null,
  }
}

function json(res, status, body) {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' })
  res.end(text)
}
function errorBody(code, message, details = {}) {
  return { error: { code, message, details }, request_id: 'smoke' }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

/**
 * 从 multipart 里取出 `name="file"` 那一段
 *
 * 只处理 JSON 文本包，够用：我们要验证的是"字段名是 file、内容是导出包原文"。
 */
function extractFilePart(buffer, contentType) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(contentType || '')
  const boundary = (m && (m[1] || m[2]) || '').trim()
  if (!boundary) return null
  const text = buffer.toString('utf8')
  for (const part of text.split(`--${boundary}`)) {
    if (!/name="file"/.test(part)) continue
    const headEnd = part.indexOf('\r\n\r\n')
    if (headEnd < 0) continue
    const headerBlock = part.slice(0, headEnd)
    const nameMatch = /filename="([^"]*)"/i.exec(headerBlock)
    let content = part.slice(headEnd + 4)
    if (content.endsWith('\r\n')) content = content.slice(0, -2)
    return { content, fileName: nameMatch ? nameMatch[1] : '' }
  }
  return null
}

function handleUpload(req, res, body) {
  uploadCount += 1
  if (uploadCount > 5) {
    // 契约：同一 IP 每小时 5 次 → 第 6 次 429，并带 retry_after
    json(res, 429, errorBody('too_many_uploads', '上传太频繁', { retry_after: 1800 }))
    return
  }
  const file = extractFilePart(body, req.headers['content-type'])
  if (!file) {
    json(res, 400, errorBody('bad_package', '包读不出来：缺少 file 字段'))
    return
  }
  lastUpload = { fileName: file.fileName, bytes: Buffer.byteLength(file.content) }
  // 用包内容里的标记模拟三种失败，让前端的状态码 → 文案映射被真正走到
  if (file.content.includes('__too_big__')) {
    json(res, 413, errorBody('too_large', '数据包太大了（上限 64 MB）'))
    return
  }
  if (file.content.includes('__disk_full__')) {
    json(res, 507, errorBody('storage_full', '服务器存储满了'))
    return
  }
  if (file.content.includes('__broken__')) {
    json(res, 400, errorBody('bad_package', '包读不出来'))
    return
  }
  const code = `AB3D7K${String(codes.size + 1).padStart(2, '0')}`
  codes.set(code, {
    bytes: Buffer.byteLength(file.content),
    source: 'json',
    expiresAt: new Date(Date.now() + 24 * 3600 * 1000).toISOString(),
    claimedBy: new Set(),
    claimCount: 0,
  })
  json(res, 201, {
    code,
    expires_at: codes.get(code).expiresAt,
    bytes: codes.get(code).bytes,
    source: 'json',
  })
}

function handleVerify(res, code) {
  if (code === EXPIRED_CODE) {
    json(res, 410, errorBody('code_expired', '取件码已过期'))
    return
  }
  const item = codes.get(code)
  if (!item) {
    json(res, 404, errorBody('code_not_found', '取件码不存在'))
    return
  }
  json(res, 200, {
    code,
    bytes: item.bytes,
    source: item.source,
    expires_at: item.expiresAt,
    claimed: item.claimCount > 0,
    claim_count: item.claimCount,
  })
}

async function handleClaim(req, res) {
  const auth = String(req.headers.authorization || '')
  if (forceClaimUnauthorized || auth !== `Bearer ${DEVICE_TOKEN}`) {
    json(res, 401, errorBody('unauthorized', '需要登录'))
    return
  }
  let body = {}
  try {
    body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
  } catch {
    json(res, 400, errorBody('bad_request', 'body 不是 JSON'))
    return
  }
  const code = String(body.code || '').toUpperCase()
  if (code === EXPIRED_CODE) {
    json(res, 410, errorBody('code_expired', '取件码已过期'))
    return
  }
  const item = codes.get(code)
  if (!item) {
    json(res, 404, errorBody('code_not_found', '取件码不存在'))
    return
  }
  const duplicate = item.claimedBy.has(DEVICE_TOKEN)
  if (!duplicate) {
    item.claimedBy.add(DEVICE_TOKEN)
    item.claimCount += 1
  }
  latestImport = importPayload({ duplicate })
  json(res, 200, latestImport)
}

async function handleRegister(req, res) {
  let body = {}
  try {
    body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
  } catch {
    json(res, 400, errorBody('bad_request', 'body 不是 JSON'))
    return
  }
  if ('claim_code' in body) {
    const code = String(body.claim_code || '').toUpperCase()
    if (code === BAD_CLAIM_CODE || !codes.has(code)) {
      // 契约：码无效/过期 → 400 bad_claim_code，整次注册回滚
      json(res, 400, errorBody('bad_claim_code', '取件码无效或已过期'))
      return
    }
    latestImport = importPayload({})
  }
  json(res, 201, {
    user: {
      id: 'u_smoke',
      username: body.username || 'smoke',
      display_name: body.display_name || body.username || 'smoke',
      created_at: new Date().toISOString(),
    },
    adopted_conversations: 0,
  })
}

const server = http.createServer((req, res) => {
  void (async () => {
    try {
      const url = new URL(req.url, 'http://127.0.0.1')
      const pathname = url.pathname

      if (req.method === 'POST' && pathname === '/v1/auth/device') {
        json(res, 201, { device_id: 'dev_smoke', token: DEVICE_TOKEN })
        return
      }
      if (req.method === 'POST' && pathname === '/v1/uploads') {
        handleUpload(req, res, await readBody(req))
        return
      }
      if (req.method === 'GET' && pathname.startsWith('/v1/uploads/')) {
        handleVerify(res, decodeURIComponent(pathname.slice('/v1/uploads/'.length)).toUpperCase())
        return
      }
      if (req.method === 'POST' && pathname === '/v1/uploads/claim') {
        await handleClaim(req, res)
        return
      }
      if (req.method === 'POST' && pathname === '/v1/auth/register') {
        await handleRegister(req, res)
        return
      }
      if (req.method === 'GET' && pathname === '/v1/imports') {
        json(res, 200, { items: latestImport ? [latestImport] : [] })
        return
      }
      json(res, 404, errorBody('not_found', `没有这个端点：${pathname}`))
    } catch (err) {
      json(res, 500, errorBody('smoke_crash', String(err && err.stack ? err.stack : err)))
    }
  })()
})

function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve(server.address().port))
  })
}

// =============================================================================
// 2. 把前端代码打成临时模块（Node 里可直接 import）
// =============================================================================

async function bundleFrontModules() {
  const entry = `
    export * as cloud from '../src/utils/cloudData'
    export * as auth from '../src/utils/auth'
    export { friendlyError } from '../src/utils/apiClient'
  `
  const result = await esbuild.build({
    stdin: {
      contents: entry,
      resolveDir: HERE, // 与 scripts/ 同级，所以 ../src/... 就是源码目录
      sourcefile: 'claim-smoke-entry.ts',
      loader: 'ts',
    },
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'neutral',
    target: 'es2022',
    // 浏览器由 Vite 注入这两个值；Node 里给成等价常量
    define: {
      'import.meta.env.DEV': 'false',
      'import.meta.env.PROD': 'true',
      'import.meta.env.VITE_BAKER_BACKEND': 'undefined',
    },
    logLevel: 'silent',
  })
  const tmpFile = path.join(os.tmpdir(), `baker-claim-smoke-${process.pid}.mjs`)
  fs.writeFileSync(tmpFile, result.outputFiles[0].text, 'utf8')
  return tmpFile
}

// =============================================================================
// 3. 跑测试
// =============================================================================

/**
 * 装好运行环境并拿到前端模块
 *
 * @param base 后端地址（写进 localStorage 的 baker_backend_url，走前端真实的地址解析）
 */
async function prepare(base) {
  // 前端读后端地址的顺序是 localStorage 覆盖 → 构建期注入 → 同源，
  // 这里用第一种（与"控制台里 baker_backend_url 调试"完全同一条路径）
  const mem = new Map([['baker_backend_url', base]])
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
    key: (i) => [...mem.keys()][i] ?? null,
    get length() {
      return mem.size
    },
  }

  const tmpFile = await bundleFrontModules()
  try {
    return await import(pathToFileURL(tmpFile).href)
  } finally {
    fs.rmSync(tmpFile, { force: true })
  }
}

/** 假后端模式：把契约里每个分支都踩一遍（含错误码映射、限流、过期、重复领取） */
async function runFakeChecks({ cloud, auth, friendlyError }) {
  // ---- 3.1 取件码清洗 ------------------------------------------------------
  console.log('\n[1] 取件码清洗与格式校验')
  assert(cloud.normalizeClaimCode('ab 3d-7k9q') === 'AB3D7K9Q', '大小写/空格/符号被清洗成 8 位大写')
  assert(cloud.isClaimCodeValid('AB3D7K9Q'), '8 位合法码通过')
  assert(!cloud.isClaimCodeValid('AB3D7K9'), '7 位不算合法')
  assert(!cloud.isClaimCodeValid('AB3D7K9Q2'), '9 位不算合法')
  assert(cloud.formatExpiresAt('2026-11-19T00:00:00+00:00').length >= 16, '过期时间能转成本地可读格式')

  // ---- 3.2 上传：三种失败 + 成功 ------------------------------------------
  console.log('\n[2] 上传  POST /v1/uploads（multipart 字段 file）')
  await expectThrows(
    () => cloud.uploadCloudData(JSON.stringify({ __too_big__: true })),
    '413 → 提示"单份上限 64MB"',
    ['64MB'],
  )
  await expectThrows(
    () => cloud.uploadCloudData(JSON.stringify({ __disk_full__: true })),
    '507 → 提示"服务器存储满"',
    ['存储满'],
  )
  await expectThrows(
    () => cloud.uploadCloudData(JSON.stringify({ __broken__: true })),
    '400 → 提示"读不出这个包"',
    ['读不出'],
  )

  const packageJson = JSON.stringify({ version: 1, cards: [{ conversations: [] }] })
  const progressFrames = []
  const ticket = await cloud.uploadCloudData(packageJson, (p) => progressFrames.push(p))
  assert(/^[A-Z0-9]{8}$/.test(ticket.code || ''), '上传成功拿到 8 位取件码', ticket.code)
  assert(typeof ticket.expires_at === 'string' && !Number.isNaN(Date.parse(ticket.expires_at)), '返回 24 小时有效期')
  assert(ticket.bytes === Buffer.byteLength(packageJson), '服务器收到的字节数 = 导出包原文长度')
  assert(ticket.source === 'json', '服务端识别为 json 包', ticket.source)
  assert(lastUpload && lastUpload.fileName.endsWith('.json'), 'FormData 字段名是 file、文件名是 .json', lastUpload?.fileName)
  assert(
    progressFrames.some((p) => p.waitingServer || p.phase === 'upload'),
    '上传过程有进度回调（浏览器里是真实字节，Node 里是"等服务端"帧）',
    `${progressFrames.length} 帧`,
  )

  const secondTicket = await cloud.uploadCloudData(packageJson)
  assert(!!secondTicket.code && secondTicket.code !== ticket.code, '同一份包再传一次会拿到新码（可重复上传）')

  await expectThrows(
    () => cloud.uploadCloudData(packageJson),
    '429（同一 IP 每小时第 6 次）→ 提示频率与等待时长',
    ['太频繁', '分钟'],
  )

  // ---- 3.3 验码 ------------------------------------------------------------
  console.log('\n[3] 验码  GET /v1/uploads/{code}（免登录）')
  const info = await cloud.verifyUploadCode(ticket.code.toLowerCase())
  assert(info.code === ticket.code, '小写输入也能验到（前端统一大写）')
  assert(info.claimed === false && info.claim_count === 0, '还没领过：claimed=false / claim_count=0')
  await expectThrows(() => cloud.verifyUploadCode(UNKNOWN_CODE), '404 → 提示"取件码不存在"', ['不存在'])
  await expectThrows(() => cloud.verifyUploadCode(EXPIRED_CODE), '410 → 提示"已过期"', ['过期'])
  await expectThrows(() => cloud.verifyUploadCode('ABC'), '本地就拦住不足 8 位的码（不发请求）', ['8 位'])

  // ---- 3.4 领取 ------------------------------------------------------------
  console.log('\n[4] 领取  POST /v1/uploads/claim（带设备 token）')
  // 注意：这里不预先调 /auth/me —— 设备 token 由 apiClient 在**第一个鉴权请求前**
  // 自动注册（POST /v1/auth/device），claim 时应该已经带上 Bearer（正好验证这条链路）
  const claim = await cloud.claimUploadCode(ticket.code)
  assert(claim.conversations === 2 && claim.messages === 37, '领取返回导入摘要（2 段会话 / 37 条消息）')
  assert(claim.images === 3 && claim.skipped === 1, '摘要里带图片数与跳过数')
  assert(claim.duplicate === false, '第一次领取 duplicate=false')
  assert(cloud.describeClaimResult(claim).includes('段会话'), '摘要文案包含会话数', cloud.describeClaimResult(claim))

  const again = await cloud.claimUploadCode(ticket.code)
  assert(again.duplicate === true, '同一设备重复领同一份包 → duplicate=true（后端按内容去重）')
  assert(cloud.describeClaimResult(again).includes('之前领过'), 'duplicate 时文案说明"没有重复导入"')

  const infoAfter = await cloud.verifyUploadCode(ticket.code)
  assert(infoAfter.claimed === true && infoAfter.claim_count === 1, '验码能看到已被领过（claim_count=1）')

  const combined = await cloud.claimAfterVerify(secondTicket.code)
  assert(combined.conversations === 2, 'claimAfterVerify（验码 → 领取）一条龙可用')

  forceClaimUnauthorized = true
  await expectThrows(() => cloud.claimUploadCode(secondTicket.code), '401 → 提示"要先登录"', ['先登录'])
  forceClaimUnauthorized = false
  await expectThrows(() => cloud.claimUploadCode(UNKNOWN_CODE), 'claim 404 → 提示"取件码不存在"', ['不存在'])
  await expectThrows(() => cloud.claimUploadCode(EXPIRED_CODE), 'claim 410 → 提示"已过期"', ['过期'])

  // ---- 3.5 注册带码 --------------------------------------------------------
  console.log('\n[5] 注册带码  POST /v1/auth/register（claim_code 可选）')
  const registered = await auth.registerAccount('smoke_user', 'password123', '冒烟', ticket.code)
  assert(registered.user?.username === 'smoke_user', '带有效码注册 → 201 + 用户信息')
  const noCode = await auth.registerAccount('smoke_user2', 'password123', '')
  assert(noCode.user?.username === 'smoke_user2', '不带码注册照常成功（不带 claim_code 字段）')

  let badCodeError = null
  try {
    await auth.registerAccount('smoke_user3', 'password123', '', BAD_CLAIM_CODE)
  } catch (err) {
    badCodeError = err
  }
  assert(!!badCodeError, '坏码注册被拒（400）')
  assert(badCodeError?.code === 'bad_claim_code', '错误码是 bad_claim_code', badCodeError?.code)
  const friendly = friendlyError(badCodeError)
  assert(friendly.includes('没注册成功') && friendly.includes('取件码'), '注册带坏码的用户文案说清"这次没注册成功"', friendly)

  // ---- 3.6 注册后读导入记录 ------------------------------------------------
  console.log('\n[6] 注册后确认"领到了什么"  GET /v1/imports')
  const latest = await cloud.fetchLatestImport()
  assert(!!latest && latest.conversations === 2, '能读到最近一次导入（用于注册后的摘要）')
  const summary = cloud.describeClaimResult(latest, '注册时已领取：')
  assert(summary.startsWith('注册时已领取：'), '注册路径的摘要开头是"注册时已领取："', summary)
}

/**
 * 真实后端模式（`--live` 或 `BAKER_BACKEND=...`）
 *
 * 只跑"真后端一定会发生"的路径。413 / 507 / 429 / 410 这些要靠**制造事故**
 * 才能触发（塞 64MB、灌满磁盘、连传 6 次、等 24 小时），不在真后端上做，
 * 它们在假后端模式里已经覆盖过映射文案。
 */
async function runLiveChecks({ cloud, auth, friendlyError }) {
  const stamp = Date.now().toString(36)
  const pkg = JSON.stringify({
    version: 1,
    myGender: 'male',
    cards: [
      {
        members: [],
        conversations: [
          {
            name: '佩丽卡',
            messages: [
              { side: 'mine', text: '你好，佩丽卡。' },
              { side: 'other', text: '（点头）管理员，今天的日程已经排好了。' },
            ],
          },
        ],
      },
    ],
  })

  console.log('\n[1] 真实后端：上传 → 取件码 → 验码')
  const ticket = await cloud.uploadCloudData(pkg)
  assert(/^[A-Z0-9]{8}$/.test(ticket.code || ''), '上传拿到 8 位取件码', ticket.code)
  assert(!Number.isNaN(Date.parse(ticket.expires_at)), '返回可解析的 expires_at', ticket.expires_at)
  assert(ticket.bytes > 0, '返回存储字节数', `${ticket.bytes}B`)
  assert(ticket.source === 'json', '服务端识别包类型', ticket.source)

  const fresh = await cloud.verifyUploadCode(ticket.code)
  assert(fresh.claim_count === 0 && fresh.claimed === false, '新码还没被领过', `claim_count=${fresh.claim_count}`)
  await expectThrows(() => cloud.verifyUploadCode(UNKNOWN_CODE), '不存在的码 → 404 文案', ['不存在'])

  console.log('\n[2] 注册（不带码）→ 已登录设备领取 → 摘要')
  const user1 = `feclaim_${stamp}`
  const reg1 = await auth.registerAccount(user1, 'password123', '冒烟')
  assert(reg1.user?.username === user1, '注册测试账号成功', user1)
  const claim = await cloud.claimUploadCode(ticket.code)
  assert(typeof claim.conversations === 'number', '领取返回导入摘要', `conversations=${claim.conversations} messages=${claim.messages}`)
  assert(claim.duplicate === false, '第一次领取 duplicate=false')
  const again = await cloud.claimUploadCode(ticket.code)
  assert(again.duplicate === true, '同一设备重复领同一份包 → duplicate=true（后端内容哈希去重）')
  const after = await cloud.verifyUploadCode(ticket.code)
  assert(after.claimed === true && after.claim_count >= 1, '验码显示已被领取', `claim_count=${after.claim_count}`)

  // 【2026-11-18】先退出再注册第二个账号。
  // 新后端规定「一台设备同时只能挂一个账号」（否则 409 device_taken），
  // 界面上也是"退出了才看得到注册表单"，所以脚本必须照同样的顺序走。
  // 退出只把设备从账号上摘下来，设备 token 与数据都不动。
  await auth.logoutAccount()

  console.log('\n[3] 上传第二份 → 注册时填码（claim_code 主路径）')
  const ticket2 = await cloud.uploadCloudData(pkg)
  const user2 = `feclaim2_${stamp}`
  const reg2 = await auth.registerAccount(user2, 'password123', '', ticket2.code)
  assert(reg2.user?.username === user2, '带有效码注册成功（注册即领取）', user2)
  const latest = await cloud.fetchLatestImport()
  assert(!!latest, '注册后能读到那次导入记录', latest ? `${latest.conversations} 段会话 / ${latest.messages} 条消息` : 'null')
  if (latest) {
    const summary = cloud.describeClaimResult(latest, '注册时已领取：')
    assert(summary.startsWith('注册时已领取：'), '注册路径摘要可读', summary)
  }
  const after2 = await cloud.verifyUploadCode(ticket2.code)
  assert(after2.claimed === true, '注册后该码标记为已领取')

  // 同上：第 4 步要再注册一个账号，先把 user2 退掉
  await auth.logoutAccount()

  console.log('\n[4] 坏码注册必须整体回滚')
  let badCodeError = null
  try {
    await auth.registerAccount(`feclaim3_${stamp}`, 'password123', '', 'ZZZZZZZZ')
  } catch (err) {
    badCodeError = err
  }
  assert(!!badCodeError, '坏码注册被拒')
  assert(badCodeError?.code === 'bad_claim_code', '错误码是 bad_claim_code', badCodeError?.code)
  const friendly = friendlyError(badCodeError)
  assert(friendly.includes('没注册成功'), '文案说明"这次没注册成功"', friendly)
}

async function main() {
  // 真实后端模式：BAKER_BACKEND=http://ip:port 或 --live（默认打 127.0.0.1:5660）
  const liveBase = process.env.BAKER_BACKEND
  const live = !!liveBase || process.argv.includes('--live')

  let base
  if (live) {
    base = (liveBase || 'http://127.0.0.1:5660').replace(/\/+$/, '')
    console.log(`\n真实后端模式：${base}`)
  } else {
    const port = await listen(server)
    base = `http://127.0.0.1:${port}`
    console.log(`\n假后端已启动：${base}`)
  }

  const mod = await prepare(base)
  if (live) await runLiveChecks(mod)
  else await runFakeChecks(mod)

  console.log(`\n结果：${passed} 项通过，${failed} 项失败`)
  if (!live) {
    server.closeAllConnections?.()
    await new Promise((resolve) => server.close(resolve))
  }
  process.exit(failed === 0 ? 0 : 1)
}

main().catch((err) => {
  console.error('\n冒烟脚本自己挂了：', err)
  server.closeAllConnections?.()
  process.exit(1)
})
