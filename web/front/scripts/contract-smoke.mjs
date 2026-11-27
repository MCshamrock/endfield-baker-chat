#!/usr/bin/env node
// =============================================================================
// contract-smoke.mjs —— 前端中间层 × 新后端的契约冒烟测试
// -----------------------------------------------------------------------------
// 目的：不启动界面，直接用 fetch 把「中间层会打的那些请求」逐条打一遍，
// 确认它和新后端的契约没有走样。**不调用上游模型**（无 LLM 额度消耗）：
//   · 发消息一律 generate=false（只落库、不出话）
//   · 推荐 / 继续说 这类会真的调上游的端点不测（它们的请求体在代码里已对齐）
//
// 用法（后端已在 5660 起好）：
//   node scripts/contract-smoke.mjs
//   BAKER_BACKEND=http://192.168.1.5:5660 node scripts/contract-smoke.mjs
//
// 覆盖：
//   1. POST /v1/auth/device                匿名设备
//   2. GET  /v1/meta                       能力声明
//   3. GET  /v1/characters                 角色表（name → slug）
//   4. POST /v1/conversations              建单聊会话
//   5. POST /v1/imports                    历史导入（老数据包 JSON 形状）
//   6. GET  /v1/conversations/{id}/messages 导入结果（两侧都在）
//   7. POST /v1/conversations/{id}/messages generate=false（只存不发）
//   8. POST /v1/attachments                图片上传 + 去重
//   9. POST /v1/auth/register/login/me/logout  账号链路
//  10. POST /v1/auth/password              改密码
// =============================================================================

const BASE = (process.env.BAKER_BACKEND || 'http://127.0.0.1:5660').replace(/\/+$/, '')

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
  return cond
}

async function call(path, { method = 'GET', token, body, form } = {}) {
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: form ?? (body !== undefined ? JSON.stringify(body) : undefined),
  })
  let json = null
  try {
    json = await response.json()
  } catch {
    // 204 没有 body
  }
  return { status: response.status, json }
}

const stamp = Date.now().toString(36)

async function main() {
  console.log(`\n[1] 设备注册  POST ${BASE}/v1/auth/device`)
  const device = await call('/v1/auth/device', {
    method: 'POST',
    body: { platform: 'contract-smoke', app_ver: 'test', tz: 'Asia/Shanghai', locale: 'zh-CN' },
  })
  assert(device.status === 201 && !!device.json?.token, '拿到设备 token', `status=${device.status}`)
  const token = device.json?.token
  if (!token) throw new Error('没有设备 token，后续全部无法进行')

  console.log('\n[2] 元信息  GET /v1/meta')
  const meta = await call('/v1/meta', { token })
  assert(meta.status === 200, 'meta 200')
  assert(meta.json?.capabilities?.web_search === false, 'capabilities.web_search=false')
  assert(Array.isArray(meta.json?.caps), 'caps 是数组')

  console.log('\n[3] 角色表  GET /v1/characters')
  const chars = await call('/v1/characters', { token })
  const items = chars.json?.items ?? []
  assert(chars.status === 200 && items.length > 0, '角色表非空', `${items.length} 个`)
  const perlica = items.find((c) => c.id === 'perlica')
  assert(!!perlica, '包含 perlica', perlica?.name ?? '')

  console.log('\n[4] 建会话  POST /v1/conversations')
  const conv = await call('/v1/conversations', {
    method: 'POST',
    token,
    body: { character_id: 'perlica', mode: 'single', title: '契约冒烟' },
  })
  assert(conv.status === 201 && !!conv.json?.id, '建会话 201', conv.json?.id ?? '')
  assert(conv.json?.next_seq === 0, '新会话 next_seq=0（第一条消息才拿 seq=1）')
  const conversationId = conv.json?.id
  if (!conversationId) throw new Error('没有会话 id')

  console.log('\n[5] 历史导入  POST /v1/imports')
  const pkg = {
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
              { side: 'mine', text: '第一项是什么？' },
              { side: 'other', text: '先去食堂。你还没吃早饭。' },
            ],
          },
        ],
      },
    ],
  }
  const form = new FormData()
  form.append('file', new Blob([JSON.stringify(pkg)], { type: 'application/json' }), 'smoke.json')
  form.append('mode', 'merge')
  const imported = await call('/v1/imports', { method: 'POST', token, form })
  assert(imported.status === 201, '导入 201', `status=${imported.status}`)
  assert((imported.json?.messages ?? 0) >= 4, '导入消息数 ≥ 4', String(imported.json?.messages ?? 0))
  assert((imported.json?.conversations ?? 0) === 1, '导入会话数 = 1')

  console.log('\n[6] 读消息  GET /v1/conversations/{id}/messages')
  // 导入会新建一条会话，用它读
  const list = await call('/v1/conversations', { token })
  const newest = (list.json?.items ?? []).find((c) => c.character_id === 'perlica')
  const readId = newest?.id ?? conversationId
  const msgs = await call(`/v1/conversations/${readId}/messages?after_seq=0&limit=50`, { token })
  const rows = msgs.json?.items ?? []
  assert(msgs.status === 200 && rows.length >= 4, '消息 ≥ 4 条', String(rows.length))
  assert(rows.some((m) => m.side === 'assistant'), '助手侧消息存在（不是只导入 user）')
  assert(rows.every((m) => typeof m.seq === 'number'), '每条都有 seq')

  console.log('\n[7] 只存不发  POST /v1/conversations/{id}/messages generate=false')
  const stored = await call(`/v1/conversations/${readId}/messages`, {
    method: 'POST',
    token,
    body: { client_msg_id: `smoke-${stamp}`, text: '这条只存不发', generate: false },
  })
  assert(stored.status === 200 && stored.json?.duplicate === false, '写入成功', `seq=${stored.json?.seq}`)
  const again = await call(`/v1/conversations/${readId}/messages`, {
    method: 'POST',
    token,
    body: { client_msg_id: `smoke-${stamp}`, text: '这条只存不发', generate: false },
  })
  assert(again.json?.duplicate === true, '同一 client_msg_id 幂等（duplicate=true）')

  console.log('\n[8] 图片上传  POST /v1/attachments')
  // 1×1 透明 PNG
  const png = Uint8Array.from(
    atob(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    ),
    (c) => c.charCodeAt(0),
  )
  const upForm = new FormData()
  upForm.append('file', new Blob([png], { type: 'image/png' }), 'dot.png')
  const uploaded = await call('/v1/attachments', { method: 'POST', token, form: upForm })
  assert(uploaded.status === 201 && !!uploaded.json?.id, '上传 201', uploaded.json?.id ?? '')
  const upForm2 = new FormData()
  upForm2.append('file', new Blob([png], { type: 'image/png' }), 'dot.png')
  const uploaded2 = await call('/v1/attachments', { method: 'POST', token, form: upForm2 })
  assert(uploaded2.json?.dedup === true, '同图去重（dedup=true）')

  console.log('\n[9] 账号链路  register → me → logout → login')
  const username = `smoke_${stamp}`
  const password = 'smoke-pass-2026'
  const registered = await call('/v1/auth/register', {
    method: 'POST',
    token,
    body: { username, password, display_name: '冒烟测试' },
  })
  assert(registered.status === 201 && !!registered.json?.user, '注册 201', `adopted=${registered.json?.adopted_conversations}`)
  assert(typeof registered.json?.adopted_conversations === 'number', '返回 adopted_conversations')

  const me = await call('/v1/auth/me', { token })
  assert(me.json?.kind === 'user' && me.json?.user?.username === username, 'me 返回 user')
  assert(Array.isArray(me.json?.devices), 'me 带 devices')

  const loggedOut = await call('/v1/auth/logout', { method: 'POST', token })
  assert(loggedOut.status === 204, '登出 204')
  const meGuest = await call('/v1/auth/me', { token })
  assert(meGuest.json?.kind === 'guest', '登出后 me = guest')

  const relogin = await call('/v1/auth/login', { method: 'POST', token, body: { username, password } })
  assert(relogin.status === 200 && relogin.json?.user?.username === username, '重新登录 200')

  console.log('\n[10] 改密码  POST /v1/auth/password')
  const changed = await call('/v1/auth/password', {
    method: 'POST',
    token,
    body: { old_password: password, new_password: 'smoke-pass-2027' },
  })
  assert(changed.status === 204, '改密 204')
  const badOld = await call('/v1/auth/password', {
    method: 'POST',
    token,
    body: { old_password: 'wrong', new_password: 'whatever-123' },
  })
  assert(badOld.status === 401, '旧密码错 → 401')

  console.log(`\n结果：${passed} PASS / ${failed} FAIL\n`)
  process.exitCode = failed ? 1 : 0
}

main().catch((err) => {
  console.error('\n测试中断：', err)
  process.exitCode = 1
})
