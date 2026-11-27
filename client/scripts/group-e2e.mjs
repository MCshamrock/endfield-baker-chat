#!/usr/bin/env node
// =============================================================================
// group-e2e.mjs —— 群聊中间层（/v1）× 新后端 的端到端验证
// -----------------------------------------------------------------------------
// 和 adapter-e2e.mjs 同一套路：esbuild 现场打包 TS，fetch 只拦截"真正生成回复"
// 的两个端点（`/messages` 与 `/next`），其余请求全部打真后端。
//
// 覆盖：
//   1. 建群直接落 /v1（mode=group、members=slug、speak_mode=weighted）
//   2. 第一次取回复：把本地历史导入服务端（含说话人），再按"一轮全员"收集
//   3. 第二句：发消息 → 一条回复 → 继续 /next 直到 done
//   4. assign 模式：只让指定的人说一句
//   5. 发言权重：PUT /characters/{slug}/weight + 本地缓存
// =============================================================================

import { build } from 'esbuild'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const BASE = (process.env.BAKER_BACKEND || 'http://127.0.0.1:5660').replace(/\/+$/, '')

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

const workDir = await mkdtemp(join(tmpdir(), 'baker-group-'))
const bundlePath = join(workDir, 'groupBackend.mjs')

await build({
  entryPoints: ['src/utils/groupBackend.ts'],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: bundlePath,
  logLevel: 'silent',
  define: {
    'import.meta.env.DEV': 'false',
    'import.meta.env.VITE_BAKER_BACKEND': JSON.stringify(BASE),
  },
})

const memory = new Map()
globalThis.localStorage = {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => memory.set(k, String(v)),
  removeItem: (k) => memory.delete(k),
  clear: () => memory.clear(),
}
globalThis.window = globalThis
Object.defineProperty(globalThis, 'navigator', {
  value: { userAgent: 'node-group-e2e', language: 'zh-CN' },
  configurable: true,
})

// ---- 假生成：按成员顺序轮流"发言" -----------------------------------------
const MEMBERS = ['黎风', '伊冯', '骏卫']
const groupState = new Map() // conversationId → next index（下一位该谁说）
const calls = []

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url
  const method = (init.method || 'GET').toUpperCase()
  const body = init.body && typeof init.body === 'string' ? JSON.parse(init.body) : {}

  // 玩家说一句：服务端只回一条（这里也模拟"第一位"）
  if (method === 'POST' && /\/v1\/conversations\/[^/]+\/messages$/.test(url)) {
    calls.push({ kind: 'message', body })
    const id = url.split('/').slice(-2)[0]
    groupState.set(id, 1)
    return json({
      seq: 100,
      duplicate: false,
      reply: `（假回复）我是${MEMBERS[0]}。`,
      reply_seq: 101,
      speaker: MEMBERS[0],
      silent: false,
      usage: { prompt_tokens: 100, completion_tokens: 10, cached_tokens: 0, total_ms: 10 },
      created_at: new Date().toISOString(),
    })
  }

  // 下一位
  if (method === 'POST' && /\/v1\/conversations\/[^/]+\/next$/.test(url)) {
    calls.push({ kind: 'next', body })
    const id = url.split('/').slice(-2)[0]
    const index = groupState.get(id) ?? 0
    groupState.set(id, index + 1)
    // 服务端语义：说完这一位之后再算 done —— 最后一位仍然有台词，done 同时为 true
    if (index >= MEMBERS.length) {
      return json({ reply: null, reply_seq: null, speaker: '', done: true, remaining: [], created_at: new Date().toISOString() })
    }
    const speaker = MEMBERS[index]
    const done = index >= MEMBERS.length - 1
    return json({
      reply: `（假回复）我是${speaker}。`,
      reply_seq: 200 + index,
      speaker,
      done,
      remaining: done ? [] : MEMBERS.slice(index + 1),
      created_at: new Date().toISOString(),
    })
  }

  return realFetch(input, init)
}

function json(value) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const mod = await import(pathToFileURL(bundlePath).href)
const {
  createGroupSession,
  requestGroupReply,
  setCharacterWeight,
  readLocalWeights,
  resolveGroupSessionId,
} = mod

function historyOf(lines) {
  return lines.map(([side, speaker, text]) => ({ side, speaker, text }))
}

async function serverJson(path) {
  const token = localStorage.getItem('baker_device_token')
  const response = await realFetch(`${BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  return response.json()
}

console.log('\n[1] 建群（weighted）')
const created = await createGroupSession({
  members: MEMBERS,
  myRole: 'admin',
  speakMode: 'weighted',
  groupName: '加权测试群',
})
check(!!created.data?.session_id, '拿到 session_id', created.data?.session_id ?? created.error)
const convId = created.data?.session_id
const conv = await serverJson(`/v1/conversations/${convId}`)
check(conv.mode === 'group', '服务端是群聊会话')
check(conv.speak_mode === 'weighted', 'speak_mode=weighted', conv.speak_mode)
check((conv.members ?? []).length === 3, '成员 3 人', String((conv.members ?? []).length))

console.log('\n[2] 第一轮：导入历史 + 全员说完')
const history = historyOf([
  ['mine', '管理员', '今天谁值班？'],
  ['other', '黎风', '（举手）我。'],
  ['other', '伊冯', '我可以替她。'],
  ['mine', '管理员', '那就黎风吧。'],
  ['other', '黎风', '收到。'],
])
const first = await requestGroupReply(convId, history, { newTurn: true, topic: '' })
check(!!first.data, '拿到结果', first.error)
check((first.data?.replies ?? []).length === 3, '三位各说一条', String(first.data?.replies?.length))
check(first.data?.done === true, '本轮 done=true')

const activeId = resolveGroupSessionId(convId)
check(activeId !== convId, '导入后 id 已换（别名生效）', activeId)
const conv2 = await serverJson(`/v1/conversations/${activeId}`)
check(conv2.id === activeId && conv2.mode === 'group', '新会话存在')
const messages = await serverJson(`/v1/conversations/${activeId}/messages?after_seq=0&limit=100`)
const rows = messages.items ?? []
check(rows.length >= 5, '导入的历史在库里', String(rows.length))
check(rows.some((m) => m.side === 'assistant' && m.speaker === '黎风'), '说话人保留（黎风）')
check(rows.filter((m) => m.side === 'user').length >= 2, '玩家消息在')

console.log('\n[3] 第二句：发消息 + 继续 /next')
const history2 = historyOf([
  ...history.map((h) => [h.side, h.speaker, h.text]),
  ['other', '黎风', '（假回复）我是黎风。'],
  ['other', '伊冯', '（假回复）我是伊冯。'],
  ['mine', '管理员', '那骏卫呢？'],
])
const second = await requestGroupReply(convId, history2, { newTurn: true, topic: '' })
check(!!second.data, '拿到结果', second.error)
check((second.data?.replies ?? []).length === 3, '这一轮也是三人', String(second.data?.replies?.length))
check(second.data?.done === true, 'done=true')
check(calls.filter((c) => c.kind === 'message').length === 1, '只发了一次玩家消息')

console.log('\n[4] assign 模式：只让指定的人说')
const assigned = await createGroupSession({
  members: MEMBERS,
  myRole: 'admin',
  speakMode: 'assign',
  assignTarget: '骏卫',
  groupName: '指定测试群',
})
const assignId = assigned.data?.session_id
const assignReply = await requestGroupReply(
  assignId,
  historyOf([
    ['mine', '管理员', '骏卫你说。'],
  ]),
  { newTurn: true, topic: '' },
)
check(!!assignReply.data, '拿到结果', assignReply.error)
check((assignReply.data?.replies ?? []).length === 1, '只回一条', String(assignReply.data?.replies?.length))
check(assignReply.data?.done === true, 'done=true')

// 第二轮（有新的玩家消息）：这一轮会走 /messages，应带 assign_speaker
const assignHistory2 = historyOf([
  ['mine', '管理员', '骏卫你说。'],
  ['other', '骏卫', '（假回复）我是骏卫。'],
  ['mine', '管理员', '再说一句。'],
])
await requestGroupReply(assignId, assignHistory2, { newTurn: true, topic: '' })
const lastMessageCall = [...calls].reverse().find((c) => c.kind === 'message')
check(
  lastMessageCall?.body?.assign_speaker === '骏卫',
  '请求里带 assign_speaker',
  lastMessageCall?.body?.assign_speaker,
)

console.log('\n[5] 发言权重')
const weightOk = await setCharacterWeight('佩丽卡', 88)
check(weightOk, 'PUT 权重成功')
check(readLocalWeights()['佩丽卡'] === 88, '本地缓存已更新', String(readLocalWeights()['佩丽卡']))
const badWeight = await setCharacterWeight('佩丽卡', 500)
check(badWeight, '超范围值被夹到 1..100 后保存')
check(readLocalWeights()['佩丽卡'] === 100, '夹到 100', String(readLocalWeights()['佩丽卡']))

console.log(`\n结果：${passed} PASS / ${failed} FAIL`)
await rm(workDir, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
