#!/usr/bin/env node
// =============================================================================
// two-archives-e2e.mjs —— 「同一角色的两段对话互相串」的回归测试
// -----------------------------------------------------------------------------
// 事故（2026-11-19，群友权倾浊酒澄吾心）：
//   同一个角色开两段对话。第 1 段叫了声「亲爱的」，第 2 段关系没那么近，
//   结果她**直接无缝接上** —— 关了「记忆互通」也一样，100% 复现。
//
// 根因：发送链路的绑定只认角色（`baker_v1_bindings[characterId]`），
//   于是同角色的两段对话共用同一条服务端会话。
//
// 这个脚本用**假后端**把三条路径钉死（不需要浏览器、不需要真库）：
//   1. 两段对话（不同 conversationKey）→ 建出**两条**服务端会话
//   2. 各自的后续消息只进自己那条会话
//   3. 老绑定（只按角色名的键）只被认领**一次**，第二段对话不会抢到它
//
// 运行：node scripts/two-archives-e2e.mjs
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
    passed++
    console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`)
  } else {
    failed++
    console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`)
  }
  return !!cond
}

// ---------------------------------------------------------------------------
// 假后端：只实现这条链路用到的端点
// ---------------------------------------------------------------------------
const state = {
  conversations: [],          // { id, character_id, mode, title, next_seq }
  messages: new Map(),        // convId -> [{seq, text}]
  created: 0,                 // 一共建了几条会话
  imports: 0,
  posted: [],                 // 每次发消息 { convId, text }
}

let seqCounter = 0
function newConv(characterId, title) {
  const conv = {
    id: `conv-${++seqCounter}`,
    character_id: characterId,
    mode: 'single',
    title,
    next_seq: 0,
    created_at: new Date().toISOString(),
    last_active_at: new Date().toISOString(),
  }
  state.conversations.push(conv)
  state.messages.set(conv.id, [])
  state.created++
  return conv
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x')
  const send = (code, body) => {
    res.writeHead(code, { 'content-type': 'application/json' })
    res.end(JSON.stringify(body))
  }
  const chunks = []
  req.on('data', (c) => chunks.push(c))
  req.on('end', () => {
    const raw = Buffer.concat(chunks).toString('utf8')
    const p = url.pathname

    if (p === '/v1/auth/device') return send(201, { device_id: 'dev-1', token: 'tok-1' })

    if (p === '/v1/conversations' && req.method === 'GET') {
      return send(200, { items: state.conversations })
    }
    if (p === '/v1/conversations' && req.method === 'POST') {
      const body = JSON.parse(raw || '{}')
      return send(201, newConv(body.character_id, body.title || '佩丽卡'))
    }

    // 导入历史：模拟后端"新建一条装着历史的会话"
    if (p === '/v1/imports' && req.method === 'POST') {
      state.imports++
      const conv = newConv('perlica', '佩丽卡')
      conv.next_seq = 2
      state.messages.get(conv.id).push({ seq: 1, text: '(导入的历史)' })
      return send(201, { id: `imp-${state.imports}`, duplicate: false, conversations: 1, messages: 1 })
    }

    const m = p.match(/^\/v1\/conversations\/([^/]+)\/(messages|suggestions)(:stream)?$/)
    if (m) {
      const convId = decodeURIComponent(m[1])
      const conv = state.conversations.find((c) => c.id === convId)
      if (!conv) return send(404, { detail: 'no conversation' })

      if (m[2] === 'suggestions') return send(200, { items: [] })

      if (req.method === 'GET') {
        return send(200, { items: state.messages.get(convId) ?? [], next_seq: conv.next_seq })
      }

      const body = JSON.parse(raw || '{}')
      state.posted.push({ convId, text: body.text ?? '' })
      conv.next_seq += 1
      state.messages.get(convId).push({ seq: conv.next_seq, text: body.text ?? '' })
      if (/:stream$/.test(p)) {
        res.writeHead(200, { 'content-type': 'text/event-stream' })
        res.write(`data: ${JSON.stringify({ delta: '收到' })}\n\n`)
        res.write('data: [DONE]\n\n')
        return res.end()
      }
      return send(200, { reply: { seq: conv.next_seq + 1, text: '收到' }, next_seq: conv.next_seq + 1 })
    }

    send(404, { detail: 'not found' })
  })
})

async function bundle() {
  const entry = `
    export * as backend from '../src/utils/backend'
  `
  const result = await esbuild.build({
    stdin: { contents: entry, resolveDir: HERE, sourcefile: 'two-archives-entry.ts', loader: 'ts' },
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
  const tmp = path.join(os.tmpdir(), `baker-two-archives-${process.pid}.mjs`)
  fs.writeFileSync(tmp, result.outputFiles[0].text, 'utf8')
  return tmp
}

const mem = new Map()
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

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = `http://127.0.0.1:${server.address().port}`
mem.set('baker_backend_url', base)

const tmp = await bundle()
const { backend } = await import(pathToFileURL(tmp).href)
fs.rmSync(tmp, { force: true })

try {
  console.log(`\n假后端：${base}`)

  // ---- 1. 两段对话各自第一次说话 → 各建一条服务端会话 ----
  console.log('\n[1] 同一角色的两段对话')
  const reqA = backend.buildBackendRequest('亲爱的', '佩丽卡', [{ side: 'mine', text: '亲爱的' }], {
    characterId: 'perlica',
    conversationKey: 'c-archive-A',
  })
  await backend.fetchBackendReply({}, reqA)
  const reqB = backend.buildBackendRequest('你好', '佩丽卡', [{ side: 'mine', text: '你好' }], {
    characterId: 'perlica',
    conversationKey: 'c-archive-B',
  })
  await backend.fetchBackendReply({}, reqB)

  console.log('    DEBUG posted =', JSON.stringify(state.posted.map(x => [x.convId, x.text])))
  assert(state.created === 2, '两段对话建了两条服务端会话', `建了 ${state.created} 条`)
  const postA = state.posted[0]
  const postB = state.posted[1]
  assert(!!postA && !!postB && postA.convId !== postB.convId, '两次发送落在不同会话', `${postA?.convId} vs ${postB?.convId}`)

  // ---- 2. 第二段对话里不该出现第一段的内容 ----
  console.log('\n[2] 上下文隔离')
  const msgsB = state.messages.get(postB.convId) ?? []
  assert(!msgsB.some((m) => String(m.text).includes('亲爱的')), 'B 的会话里没有 A 说过的「亲爱的」', `B 有 ${msgsB.length} 条`)

  // ---- 3. 各自再说一句 → 仍进各自那条 ----
  console.log('\n[3] 第二轮仍归位')
  state.posted.length = 0
  await backend.fetchBackendReply({}, backend.buildBackendRequest('在吗', '佩丽卡', [], {
    characterId: 'perlica',
    conversationKey: 'c-archive-A',
  }))
  await backend.fetchBackendReply({}, backend.buildBackendRequest('我们什么关系', '佩丽卡', [], {
    characterId: 'perlica',
    conversationKey: 'c-archive-B',
  }))
  assert(state.posted[0]?.convId === postA.convId, 'A 的第二句回到 A 的会话', String(state.posted[0]?.convId))
  assert(state.posted[1]?.convId === postB.convId, 'B 的第二句回到 B 的会话', String(state.posted[1]?.convId))

  // ---- 4. 老绑定（只按角色名的键）只被认领一次 ----
  console.log('\n[4] 老绑定认领')
  mem.clear()
  mem.set('baker_backend_url', base)
  mem.set('baker_v1_bindings', JSON.stringify({ perlica: { id: 'conv-legacy', at: 1 } }))
  state.conversations.push({
    id: 'conv-legacy', character_id: 'perlica', mode: 'single', title: '佩丽卡',
    next_seq: 5, created_at: new Date().toISOString(), last_active_at: new Date().toISOString(),
  })
  state.messages.set('conv-legacy', [{ seq: 1, text: '(老历史)' }])
  const before = state.created

  await backend.fetchBackendReply({}, backend.buildBackendRequest('第一段', '佩丽卡', [], {
    characterId: 'perlica', conversationKey: 'c-old-A',
  }))
  const claimed = state.posted[state.posted.length - 1].convId
  assert(claimed === 'conv-legacy', '第一段对话认领了老会话（历史不丢）', claimed)

  await backend.fetchBackendReply({}, backend.buildBackendRequest('第二段', '佩丽卡', [], {
    characterId: 'perlica', conversationKey: 'c-old-B',
  }))
  const second = state.posted[state.posted.length - 1].convId
  assert(second !== 'conv-legacy', '第二段对话不会抢同一条老会话（这正是"串"的入口）', second)
  assert(state.created === before + 1, '第二段对话自己新建了一条', `新增 ${state.created - before} 条`)

  // ---- 5. 绑定表里不再有"纯角色名"的键（认领后已删） ----
  console.log('\n[5] 绑定表')
  const map = JSON.parse(mem.get('baker_v1_bindings') ?? '{}')
  assert(!map.perlica, '老键已删除（不会再有第二次认领）', Object.keys(map).join(', '))
  assert(!!map['perlica#c-old-A'] && !!map['perlica#c-old-B'], '两个新键各指一条会话')
} catch (err) {
  failed++
  console.log('  FAIL  异常：', err?.stack || err)
} finally {
  server.close()
}

console.log(`\n合计：${passed} PASS / ${failed} FAIL`)
process.exit(failed === 0 ? 0 : 1)
