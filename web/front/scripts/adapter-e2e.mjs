#!/usr/bin/env node
// =============================================================================
// adapter-e2e.mjs —— 中间层（src/utils/backend.ts）× 新后端的端到端验证
// -----------------------------------------------------------------------------
// 为什么需要它：契约冒烟（contract-smoke.mjs）只证明"端点能打"，
// 证明不了中间层自己的那三件事 ——
//   · 第一次见到角色时会把 history 导入成服务端会话；
//   · 当前轮发出去的请求里**没有 history**（状态在服务端）；
//   · 一轮成功后幂等键会清掉（同一句话再说一次是新消息），失败才复用。
//
// 做法：
//   1. 用 esbuild 把 TS 中间层打成 ESM；
//   2. Node 里补一个 localStorage 假实现，把 fetch 包一层 ——
//      只拦截「真正生成回复」的那一次 POST，返回一个假回复；
//      其余请求（设备注册 / 会话 / 导入 / 附件 / 读消息）全部打真后端。
//   3. 逐条断言。
//
// 用法（后端已在 5660 起好）：
//   node scripts/adapter-e2e.mjs
//   BAKER_BACKEND=http://192.168.1.5:5660 node scripts/adapter-e2e.mjs
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

// ---- 1. 打包中间层 ---------------------------------------------------------
const workDir = await mkdtemp(join(tmpdir(), 'baker-adapter-'))
const bundlePath = join(workDir, 'backend.mjs')

await build({
  entryPoints: ['src/utils/backend.ts'],
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

// ---- 2. 浏览器环境替身 -----------------------------------------------------
const memory = new Map()
globalThis.localStorage = {
  getItem: (key) => (memory.has(key) ? memory.get(key) : null),
  setItem: (key, value) => memory.set(key, String(value)),
  removeItem: (key) => memory.delete(key),
  clear: () => memory.clear(),
}
globalThis.window = globalThis
// Node 22 自带只读的 navigator：用 defineProperty 覆盖，而不是直接赋值
Object.defineProperty(globalThis, 'navigator', {
  value: { userAgent: 'node-adapter-e2e', language: 'zh-CN' },
  configurable: true,
})

// ---- 3. 拦截"生成回复"的那一次 POST ---------------------------------------
const posted = []
const realFetch = globalThis.fetch.bind(globalThis)

globalThis.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url
  const method = (init.method || 'GET').toUpperCase()
  if (method === 'POST' && /\/v1\/conversations\/[^/]+\/messages$/.test(url)) {
    const body = JSON.parse(init.body)
    posted.push(body)
    return new Response(
      JSON.stringify({
        seq: 100 + posted.length,
        duplicate: false,
        reply: `（假回复 ${posted.length}）今天吃面包。`,
        reply_seq: 200 + posted.length,
        speaker: '',
        silent: false,
        usage: { prompt_tokens: 1000, completion_tokens: 20, cached_tokens: 800, total_ms: 1234 },
        created_at: new Date().toISOString(),
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  }
  return realFetch(input, init)
}

const { fetchBackendReply } = await import(pathToFileURL(bundlePath).href)

const history = [
  { role: 'user', content: '你好，佩丽卡。' },
  { role: 'assistant', content: '（点头）管理员，今天的日程已经排好了。' },
  { role: 'user', content: '第一项是什么？' },
  { role: 'assistant', content: '先去食堂。你还没吃早饭。' },
]

console.log('\n[1] 第一次对话：应触发一次历史导入')
const first = await fetchBackendReply(
  {},
  { character: '佩丽卡', character_id: 'perlica', message: '今天吃什么？', history },
)
check(first.reply.includes('假回复'), '拿到回复', first.reply)
check(posted.length === 1, '只发了一次生成请求')
check(posted[0]?.text === '今天吃什么？', '请求体里是当前消息')
check(!('history' in (posted[0] ?? {})), '请求体里没有 history（状态在服务端）')
check(typeof posted[0]?.client_msg_id === 'string', '带 client_msg_id', posted[0]?.client_msg_id)
check(Number(first.usage?.prompt_tokens) === 1000, 'usage 映射正确')

console.log('\n[2] 服务端会话：历史确实进库了（两侧都在）')
const token = localStorage.getItem('baker_device_token')
const convList = await (
  await realFetch(`${BASE}/v1/conversations`, { headers: { Authorization: `Bearer ${token}` } })
).json()
const perlicaConv = (convList.items ?? []).filter((c) => c.character_id === 'perlica')
check(perlicaConv.length === 1, '只绑定了一条佩丽卡会话', String(perlicaConv.length))
const convId = perlicaConv[0]?.id
const rows = (
  await (
    await realFetch(`${BASE}/v1/conversations/${convId}/messages?after_seq=0&limit=100`, {
      headers: { Authorization: `Bearer ${token}` },
    })
  ).json()
).items ?? []
check(
  rows.filter((m) => m.side === 'user').length >= history.filter((m) => m.role === 'user').length,
  '导入的 user 消息在',
  String(rows.length),
)
check(rows.some((m) => m.side === 'assistant'), '导入的 assistant 消息在')

console.log('\n[3] 第二句：复用绑定、不再导入；幂等键是新的')
const second = await fetchBackendReply(
  {},
  { character: '佩丽卡', character_id: 'perlica', message: '那就走吧。', history: [...history,
    { role: 'user', content: '今天吃什么？' },
    { role: 'assistant', content: first.reply },
  ] },
)
check(second.reply.includes('假回复 2'), '第二轮拿到回复')
check(posted.length === 2, '仍然只有两次生成请求')
check(posted[1]?.client_msg_id !== posted[0]?.client_msg_id, '新的一句话用了新的幂等键')
const convList2 = await (
  await realFetch(`${BASE}/v1/conversations`, { headers: { Authorization: `Bearer ${token}` } })
).json()
check(
  (convList2.items ?? []).filter((c) => c.character_id === 'perlica').length === 1,
  '没有重复建会话',
)

console.log('\n[4] 同一句话再说一次：成功后幂等键已清，必须是新键')
await fetchBackendReply(
  {},
  { character: '佩丽卡', character_id: 'perlica', message: '今天吃什么？', history },
)
check(
  posted[2]?.client_msg_id !== posted[0]?.client_msg_id,
  '重复内容不会被当成"重发"',
  posted[2]?.client_msg_id,
)

console.log('\n[5] 图片轮：当前轮的图会上传成附件')
const pngDataUrl =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
await fetchBackendReply(
  {},
  {
    character: '佩丽卡',
    character_id: 'perlica',
    message: '[图片]',
    history: [...history, { role: 'user', content: '[图片]', image: pngDataUrl }],
  },
)
const imageBody = posted[posted.length - 1]
check(typeof imageBody?.attachment_id === 'string' && imageBody.attachment_id.length > 0,
  '带 attachment_id', imageBody?.attachment_id)

console.log('\n[6] 图片只挂当前轮：历史图不重复上传（请求体里没有 image 字段）')
check(!('image' in (imageBody ?? {})), '请求体里没有 base64 图')

console.log(`\n结果：${passed} PASS / ${failed} FAIL`)
await rm(workDir, { recursive: true, force: true })
process.exitCode = failed ? 1 : 0
