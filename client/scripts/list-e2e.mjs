#!/usr/bin/env node
// =============================================================================
// list-e2e.mjs —— 服务端列表（P1）的自测：6 条断言
// -----------------------------------------------------------------------------
// 不需要真后端、不需要浏览器：起一个假后端（Node 内置 http），把前端**真正会
// 跑的那些函数**用 esbuild 现打成临时模块直接调用 —— 与 claim-smoke 同一套路：
//   · utils/conversationSync.ts（拉列表 / 拉消息 / 拼卡树）
//   · utils/backend.ts        （发送链路：绑定 → POST messages）
//
// 六条断言（对应 docs/07 §4 P1 验收）：
//   1. server 模式下列表真的来自 /v1（2 条会话 → 1 张单聊卡 + 1 张群聊卡）
//   2. character_id 能映射成角色名（头像查不到时给默认占位）
//   3. 子卡的 remoteId === 服务端会话 id
//   4. 点进会话 → GET /messages 带上 after_seq / limit，消息能从服务端填回来
//   5. 发送 → POST 打到**选中的那条会话**（绑定生效，不是"猜最近一条"）
//   6. 领取后刷新 → 列表多一条（这是本次改动的验收核心）
//   7. 断网（fetch 抛错）→ 有缓存就画缓存 + offline 标记，不抛异常
//
// 运行：node scripts/list-e2e.mjs
// =============================================================================

import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const HERE = path.dirname(fileURLToPath(import.meta.url))

let passed = 0
let failed = 0
function assert(cond, name, detail = '') {
  if (cond) {
    passed += 1
    console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`)
  } else {
    failed += 1
    console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`)
  }
  return !!cond
}

// ---------------------------------------------------------------------------
// 假后端：只实现列表 / 消息 / 发送用到的几个端点
// ---------------------------------------------------------------------------

/** 服务端状态（测试过程中会被改写，模拟"领取后多一条会话"） */
const state = {
  conversations: [
    {
      id: 'conv-single-1',
      character_id: 'perlica',
      mode: 'single',
      title: '佩丽卡',
      members: [],
      next_seq: 3,
      created_at: '2026-11-17T10:00:00+08:00',
      last_active_at: '2026-11-18T00:30:00+08:00',
    },
    {
      id: 'conv-group-1',
      character_id: null,
      mode: 'group',
      title: '佩丽卡、伊冯的群聊',
      members: ['佩丽卡', '伊冯'],
      next_seq: 2,
      created_at: '2026-11-17T11:00:00+08:00',
      last_active_at: '2026-11-17T23:00:00+08:00',
    },
  ],
  characters: [
    { id: 'perlica', name: '佩丽卡', avatar: '', attribute: 'cryo', active_weight: 50 },
    { id: 'yifeng', name: '伊冯', avatar: '', attribute: 'pyro', active_weight: 50 },
  ],
  /** 每条会话的消息（seq 升序） */
  messages: {
    'conv-single-1': [
      { seq: 1, side: 'user', text: '你好，佩丽卡。' },
      { seq: 2, side: 'assistant', text: '（点头）管理员，今天的日程已经排好了。' },
    ],
    'conv-group-1': [
      { seq: 1, side: 'user', text: '大家早。' },
    ],
  },
  /** 请求记录（断言用） */
  seen: [],
  /** 让下一次请求失败（模拟断网） */
  failNext: false,
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  const send = (status, body) => {
    const text = JSON.stringify(body)
    res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text) })
    res.end(text)
  }
  state.seen.push({ method: req.method, path: url.pathname, query: url.search })

  if (state.failNext) {
    res.writeHead(503)
    res.end('down')
    return
  }

  // 设备注册（apiClient 需要）
  if (url.pathname === '/v1/auth/device' && req.method === 'POST') {
    send(201, { device_id: 'dev-list-e2e', token: 'tok-list-e2e' })
    return
  }
  if (url.pathname === '/v1/characters' && req.method === 'GET') {
    send(200, { items: state.characters })
    return
  }
  if (url.pathname === '/v1/conversations' && req.method === 'GET') {
    send(200, { items: state.conversations })
    return
  }
  const msgMatch = url.pathname.match(/^\/v1\/conversations\/([^/]+)\/messages$/)
  if (msgMatch) {
    const id = decodeURIComponent(msgMatch[1])
    if (req.method === 'GET') {
      const afterSeq = Number(url.searchParams.get('after_seq') ?? 0)
      const limit = Number(url.searchParams.get('limit') ?? 50)
      const all = state.messages[id] ?? []
      const items = all.filter((m) => m.seq > afterSeq).slice(0, limit)
      send(200, { items, next_after_seq: items.length ? items[items.length - 1].seq : afterSeq })
      return
    }
    if (req.method === 'POST') {
      let raw = ''
      req.on('data', (c) => (raw += c))
      req.on('end', () => {
        const body = raw ? JSON.parse(raw) : {}
        const all = state.messages[id] ?? []
        const seq = all.length + 1
        all.push({ seq, side: 'user', text: body.text ?? '' })
        const replySeq = seq + 1
        all.push({ seq: replySeq, side: 'assistant', text: '嗯。' })
        state.messages[id] = all
        send(200, {
          seq,
          reply: '嗯。',
          reply_seq: replySeq,
          duplicate: false,
          speaker: '',
          usage: { prompt_tokens: 10, completion_tokens: 2, total_tokens: 12 },
        })
      })
      return
    }
  }
  send(404, { detail: 'not found' })
})

/** 把前端模块打成一个临时 ESM（浏览器全局用 define 顶掉） */
async function bundleFrontModules() {
  const entry = `
    export * as sync from '../src/utils/conversationSync'
    export * as backend from '../src/utils/backend'
    export * as gate from '../src/utils/listSource'
    export * as mention from '../src/utils/mention'
    export * as cloud from '../src/utils/cloudPolicy'
  `
  const result = await esbuild.build({
    stdin: { contents: entry, resolveDir: HERE, sourcefile: 'list-e2e-entry.ts', loader: 'ts' },
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'neutral',
    target: 'es2022',
    define: {
      'import.meta.env.DEV': 'false',
      'import.meta.env.PROD': 'true',
      'import.meta.env.VITE_BAKER_BACKEND': 'undefined',
    },
    logLevel: 'silent',
  })
  const tmpFile = path.join(os.tmpdir(), `baker-list-e2e-${process.pid}.mjs`)
  fs.writeFileSync(tmpFile, result.outputFiles[0].text, 'utf8')
  return tmpFile
}

/** 装运行环境（内存 localStorage + 后端地址），返回前端模块 */
async function prepare(base) {
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

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = `http://127.0.0.1:${server.address().port}`
console.log(`\n假后端已启动：${base}`)

const { sync, backend, gate, mention, cloud } = await prepare(base)

try {
  // ---- 1. 列表来自 /v1，拼成卡树 ----
  console.log('\n[1] 列表  GET /v1/conversations → 卡树')
  const conversations = await sync.fetchServerConversations()
  const characters = await sync.fetchServerCharacters()
  const cards = sync.buildServerCards(conversations, characters)
  const singleCard = cards.find((c) => (c.members ?? []).length === 0 && c.conversations.some((x) => x.remoteId === 'conv-single-1'))
  const groupCard = cards.find((c) => (c.members ?? []).length >= 2)
  assert(conversations.length === 2, '列表读到 2 条服务端会话', `${conversations.length} 条`)
  assert(!!singleCard, '单聊会话 → 一张单聊卡')
  assert(!!groupCard && groupCard.conversations[0]?.remoteId === 'conv-group-1', '群聊会话 → 群聊卡（成员/会话 id 都在）', groupCard ? groupCard.members.join('、') : '(没有)')

  // ---- 2. 角色映射 + 空角色占位 ----
  console.log('\n[2] 角色映射 / 空角色占位')
  assert(singleCard?.conversations[0]?.name === '佩丽卡', 'character_id 映射成角色名', singleCard?.conversations[0]?.name ?? '')
  const placeholder = cards.find((c) => (c.members ?? []).length === 0 && c.conversations[0]?.name === '伊冯')
  assert(!!placeholder && !placeholder.conversations[0].remoteId, '没聊过的角色保留占位卡（无 remoteId，发第一句才建会话）')
  const totalPlaceholders = cards.filter((c) => !(c.members ?? []).length && !c.conversations.some((x) => x.remoteId)).length
  assert(totalPlaceholders === 1, '占位卡数量 = 没有会话的角色数', String(totalPlaceholders))

  // ---- 3. 子卡 remoteId ----
  console.log('\n[3] 子卡 remoteId')
  const sub = singleCard.conversations[0]
  assert(sub.remoteId === 'conv-single-1', '子卡的 remoteId = 服务端会话 id', sub.remoteId ?? '')
  assert(sub.remoteNextSeq === 3, '子卡记住 next_seq（拉历史算起点用）', String(sub.remoteNextSeq))

  // ---- 4. 点进会话 → 拉消息 ----
  console.log('\n[4] 点进会话  GET /v1/conversations/{id}/messages')
  const got = await sync.fetchConversationMessages('conv-single-1', 3, 50)
  const lastSeen = [...state.seen].reverse().find((r) => r.method === 'GET' && r.path.endsWith('/messages'))
  assert(!!lastSeen && lastSeen.query.includes('after_seq=0') && lastSeen.query.includes('limit=50'), '请求带上 after_seq / limit', lastSeen?.query ?? '')
  assert(got.messages.length === 2, '拉到 2 条消息', `${got.messages.length} 条`)
  const asChat = got.messages.map(sync.toChatMessage)
  assert(asChat[0].side === 'mine' && asChat[1].side === 'other', 'side 映射 user→mine / assistant→other', `${asChat[0].side}/${asChat[1].side}`)
  assert(got.nextAfterSeq === 2, 'next_after_seq 收敛到最后一条 seq', String(got.nextAfterSeq))

  // ---- 5. 发送打到选中的那条会话 ----
  console.log('\n[5] 发送  POST /v1/conversations/{id}/messages（绑定生效）')
  // 列表点击时做的事情：把「角色 → 会话」绑定指向选中的那一段
  backend.bindConversation('perlica', 'conv-single-1')
  const apiConfig = { apiMode: 'backend' }
  const request = backend.buildBackendRequest('晚上好。', '佩丽卡', [])
  const reply = await backend.fetchBackendReply(apiConfig, request)
  const posted = state.seen.filter((r) => r.method === 'POST' && /\/messages$/.test(r.path))
  assert(!!posted.length && posted.every((r) => r.path === '/v1/conversations/conv-single-1/messages'), 'POST 打到选中的会话（不是另建一条）', posted.map((p) => p.path).join(', '))
  assert(reply.reply === '嗯。', '拿到回复', reply.reply)

  // ---- 6. 领取后刷新 → 多一条 ----
  console.log('\n[6] 领取取件码后 refresh() → 列表多一条')
  const before = (await sync.fetchServerConversations()).length
  state.conversations = [
    {
      id: 'conv-claimed-1',
      character_id: 'yifeng',
      mode: 'single',
      title: '伊冯',
      members: [],
      next_seq: 5,
      created_at: '2026-11-18T01:00:00+08:00',
      last_active_at: '2026-11-18T01:10:00+08:00',
    },
    ...state.conversations,
  ]
  const after = (await sync.fetchServerConversations()).length
  const cardsAfter = sync.buildServerCards(await sync.fetchServerConversations(), await sync.fetchServerCharacters())
  const claimedCard = cardsAfter.find((c) => c.conversations.some((x) => x.remoteId === 'conv-claimed-1'))
  assert(after === before + 1, '刷新后会话数 +1', `${before} → ${after}`)
  assert(!!claimedCard && claimedCard.conversations[0].name === '伊冯', '导入的会话立刻能在卡树里找到（这就是"领取≠列表出现"的修复）')
  assert(!cardsAfter.some((c) => !(c.members ?? []).length && c.conversations[0]?.remoteId == null && c.conversations[0]?.name === '伊冯'), '伊冯的占位卡被真实会话取代（不会重复出现）')

  // ---- 7. 断网：缓存 + offline，不抛 ----
  console.log('\n[7] 断网（503）→ 不抛异常，交给调用方标记 offline')
  state.failNext = true
  let threw = false
  try {
    await sync.fetchServerConversations()
  } catch {
    threw = true
  }
  assert(threw, '拉列表失败会抛错（由 list store 捕获并标 offline）')
  const cachedCards = sync.buildServerCards(conversations, characters)
  assert(cachedCards.length > 0, '有缓存时仍能画出列表（不静默空白）', `${cachedCards.length} 张卡`)
  state.failNext = false

  // ---- 8. 本地优先（2026-11-20）：任何设备默认本机，云端不再有门槛 ----
  // 口径变了：
  //   · 以前"新设备默认 server / 有旧数据不许切"是为了"整棵替换本地卡树"那版；
  //   · 现在切云端只是"把云端没有的补进本机"（mergeServerCards 只加不改），
  //     本机永远有一份完整副本，所以默认一律本机、切云端一律允许。
  console.log('\n[8] 数据源策略：默认本机 / 切云端不设门槛')
  const freshDevice = { hasLegacyData: false, migrated: false }
  const legacyDevice = { hasLegacyData: true, migrated: false }
  assert(gate.pickInitialSource(false) === 'local', '新设备首次打开默认本机（本地优先）')
  assert(gate.pickInitialSource(true) === 'local', '有旧数据的设备默认本机')
  assert(gate.pickInitialSource(null) === 'local', '还没检测完也默认本机')
  assert(gate.canUseServerSource(freshDevice) === true, '切云端：新设备允许')
  assert(gate.canUseServerSource(legacyDevice) === true, '切云端：有旧数据没迁移也允许（本机不会被覆盖）')
  assert(
    gate.shouldAutoSwitchOnLogin({ userSet: false, requestedSource: 'local' }, freshDevice) === false,
    '登录不再自动切视图（只上传 + 补齐）',
  )

  // ---- 9. 切换判定：先判定后写入（这条规则保留，不再有"被拒"分支）----
  console.log('\n[9] 切换判定：ok/write 一致，调用方按它写存储')
  for (const dev of [legacyDevice, freshDevice]) {
    const plan = gate.planSetSource('server', dev)
    assert(plan.ok === true && plan.write === true, '切云端：ok=true 且 write=true')
  }
  assert(gate.planSetSource('local', legacyDevice).write === true, '切回本机永远允许）')
  const bogus = gate.planSetSource('nope', legacyDevice)
  assert(bogus.ok === false && bogus.write === false, '非法值：ok=false 且 write=false（不许碰存储）')

  // ---- 9b. 本地优先的核心：云端合并进本机，只加不改（2026-11-20）----
  // 这是"记录没了"那批问题的正解：server 模式的人也永远有本地副本。
  console.log('\n[9b] mergeServerCards：云端只补不删')
  const localCards = [{
    conversations: [{
      name: '佩丽卡',
      messages: [
        { id: 1, side: 'mine', text: '本地这句话不能丢' },
        { id: 2, side: 'other', text: '也不会丢' },
      ],
    }],
  }]
  const serverCards = [{
    conversations: [
      // 同一条会话（条数与服务端 next_seq-1 对得上）：应被"认领"，消息不动
      { name: '佩丽卡', messages: [], remoteId: 'srv-perlica-1', remoteNextSeq: 3, remoteTime: '2026-11-20T00:00:00Z' },
      // 服务端独有的一段：应被追加
      { name: '佩丽卡·夜谈', messages: [], remoteId: 'srv-perlica-2', remoteNextSeq: 1 },
    ],
  }, {
    // 服务端独有的角色：整张卡追加
    conversations: [{ name: '陈千语', messages: [], remoteId: 'srv-chen-1', remoteNextSeq: 1 }],
  }]
  const merged = sync.mergeServerCards(localCards, serverCards)
  assert(merged.length === 2, '合并后卡数 = 本机 1 + 云端独有 1', `${merged.length}`)
  assert(merged[0].conversations.length === 2, '同一张卡下补进服务端独有的一段', `${merged[0].conversations.length}`)
  assert(
    merged[0].conversations[0].messages.length === 2,
    '本机原有的两条消息一条没少',
    `${merged[0].conversations[0].messages.length}`,
  )
  assert(
    merged[0].conversations[0].remoteId === 'srv-perlica-1',
    '老存档用"条数对得上"认领到服务端会话 id',
    String(merged[0].conversations[0].remoteId),
  )
  assert(
    merged[0].conversations[0].remoteLoaded === true,
    '认领后标记"已拉过"，避免下次把本地消息整段换掉',
  )
  assert(localCards[0].conversations[0].remoteId === undefined, '合并不改调用方传进来的本地树（纯函数）')

  // ---- 9c. 登录后自动上传的判定（纯函数）----
  console.log('\n[9c] planUploadOnLogin：登录后自动上传的边界')
  assert(
    cloud.planUploadOnLogin({
      accountState: 'guest', hasLocalData: true, localMessages: 10,
      uploadedUserId: null, currentUserId: '',
    }).run === false,
    '没登录不传',
  )
  assert(
    cloud.planUploadOnLogin({
      accountState: 'user', hasLocalData: false, localMessages: 0,
      uploadedUserId: null, currentUserId: '7',
    }).run === false,
    '本机没有记录不传（空包没意义）',
  )
  assert(
    cloud.planUploadOnLogin({
      accountState: 'user', hasLocalData: true, localMessages: 0,
      uploadedUserId: null, currentUserId: '7',
    }).run === false,
    '只有空卡（0 条消息）也不传',
  )
  assert(
    cloud.planUploadOnLogin({
      accountState: 'user', hasLocalData: true, localMessages: 10,
      uploadedUserId: null, currentUserId: '7',
    }).run === true,
    '已登录 + 本机有记录 + 这个账号没传过 → 传',
  )
  assert(
    cloud.planUploadOnLogin({
      accountState: 'user', hasLocalData: true, localMessages: 10,
      uploadedUserId: '7', currentUserId: '7',
    }).run === false,
    '同一个账号已经传过 → 不重复传（重复整包会在云端建重复会话）',
  )
  assert(
    cloud.planUploadOnLogin({
      accountState: 'user', hasLocalData: true, localMessages: 10,
      uploadedUserId: '7', currentUserId: '8',
    }).run === true,
    '换了账号 → 允许再传一次',
  )

  // ---- 10. @ 提及：最长匹配 + 名字边界（审计 D4）----
  // 旧实现用 includes，"@明河" 会连成员「明」一起塞进 mentions（误伤没被点名的人）。
  console.log('\n[10] @ 提及解析：最长匹配 / 不误伤前缀名')
  const members = ['明', '明河', '陈千语']
  const slugOf = { 明: 'ming', 明河: 'minghe', 陈千语: 'chenqianyu' }
  assert(
    JSON.stringify(mention.extractMentionSlugs('@明河 你好', members, slugOf)) === JSON.stringify(['minghe']),
    '@明河 → 只命中「明河」，不再连「明」一起算',
    JSON.stringify(mention.extractMentionSlugs('@明河 你好', members, slugOf)),
  )
  assert(
    JSON.stringify(mention.extractMentionSlugs('@明 你好', members, slugOf)) === JSON.stringify(['ming']),
    '@明 → 命中「明」',
  )
  assert(
    JSON.stringify(mention.extractMentionSlugs('@明河 @陈千语 你们来', members, slugOf))
      === JSON.stringify(['minghe', 'chenqianyu']),
    '一句里两个提及都识别',
  )
  assert(
    mention.extractMentionSlugs('没有提及', members, slugOf).length === 0,
    '没有 @ 时返回空数组',
  )
  // 注意：输入是"@明后半句"（紧贴，没有空格）—— 插入后恰好是「@明河 后半句」
  const ins = mention.insertMention('前半句 @明后半句', '前半句 @明'.length, '明河')
  assert(ins.text === '前半句 @明河 后半句', '插入只替换 @query 那一段（不是整条）', JSON.stringify(ins.text))

  // ---- 11. 显式选过 local 的用户，登录后不被自动改成 server（审计 D5）----
  console.log('\n[11] 登录不再改变"列表从哪读"')
  const freshGate = { hasLegacyData: false, migrated: false }
  assert(
    gate.shouldAutoSwitchOnLogin({ userSet: true, requestedSource: 'local' }, freshGate) === false,
    '显式选过本机 → 登录后不会切走（2026-11-20：谁都不会切）',
  )
  assert(
    gate.shouldAutoSwitchOnLogin({ userSet: false, requestedSource: 'local' }, freshGate) === false,
    '从没选过也不切：登录只做"上传 + 云端补齐"，不换视图',
  )
  assert(
    gate.shouldAutoSwitchOnLogin({ userSet: false, requestedSource: 'local' }, { hasLegacyData: true, migrated: false }) === false,
    '有旧数据时更不会切',
  )

  console.log(`\n结果：${passed} PASS / ${failed} FAIL`)
} finally {
  server.closeAllConnections?.()
  await new Promise((resolve) => server.close(resolve))
}
process.exit(failed === 0 ? 0 : 1)
