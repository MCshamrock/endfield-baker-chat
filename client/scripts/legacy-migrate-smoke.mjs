// =============================================================================
// legacy-migrate-smoke.mjs —— 老本地数据迁移的自测（不装任何依赖，node 直跑）
// -----------------------------------------------------------------------------
// 覆盖两层：
//   A. 纯转换：手写的老数据样本（形状照 v322kit 3.2.2 的持久化层还原）
//      · 消息条数、side→role、topic→system、isError 丢弃、图片 dataURL 保留
//      · contextHistory 兜底规则、分片 + 旧快照的并集合并、自动分批
//   B. HTTP：起一个假后端（node:http），把 core 的 runMigration 真跑一遍
//      · Bearer / multipart / mode=merge / 包内容
//      · 成功写标记、第二次不重复导、413 映射成 too_large 且不写标记、分批累加
//
// 运行：node scripts/legacy-migrate-smoke.mjs
// 说明：依赖 Node 22+ 的原生 TS 类型剥离（直接 import ../src/utils/legacyLocalCore.ts）。
// =============================================================================

import http from 'node:http'
import assert from 'node:assert/strict'

import {
  buildImportPackage,
  countLegacyCards,
  mergeLegacyCards,
  runMigration,
  splitImportPackage,
  describeMigration,
  describeMigrationError,
  LegacyMigrationError,
  MIGRATE_MARKER_KEY,
} from '../src/utils/legacyLocalCore.ts'

// ---------------------------------------------------------------------------
// 手写样本：形状来自老源码里的真实默认值 / 读写处
// ---------------------------------------------------------------------------

/** 一张 1x1 透明的 dataURL（当"图还在不在"的判据，内容不重要） */
const PNG_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

/** 样本 1：单聊带图 + 失败气泡 + 空消息 + AI 记忆 */
const SINGLE_WITH_IMAGE = {
  conversations: [
    {
      name: '伊冯',
      messages: [
        { id: 1, side: 'mine', text: '管理员，早上好' },
        { id: 2, side: 'other', text: '早，今天也一起加油吧', image: PNG_DATA_URL, imageW: 240, imageH: 135 },
        { id: 3, side: 'other', text: '请求失败：网络异常', isError: true },
        { id: 4, side: 'other', text: '' },
      ],
      contextHistory: [
        { side: 'mine', text: '管理员，早上好' },
        { side: 'other', text: '早，今天也一起加油吧', image: PNG_DATA_URL },
      ],
      sessionOutputTokens: 88,
    },
  ],
}

/** 样本 2：群聊带 speaker / mood / 新话题提示行 */
const GROUP_WITH_SPEAKER = {
  members: ['伊冯', '佩丽卡'],
  myRole: 'admin',
  speakMode: 'weighted',
  conversations: [
    {
      name: '伊冯、佩丽卡',
      messages: [
        { id: 1, side: 'mine', text: '今天谁值班？', speakerName: '管理员' },
        { id: 2, side: 'other', text: '我去吧。', speakerName: '佩丽卡' },
        { id: 3, side: 'other', text: '新话题：今晚吃什么', topic: true },
        {
          id: 4,
          side: 'other',
          text: '我留下看家。',
          speakerName: '伊冯',
          mood: 'sns_emoji_001',
        },
      ],
    },
  ],
}

/** 样本 3：消息被清空过，只剩下 AI 记忆（必须由 contextHistory 兜底） */
const CONTEXT_ONLY = {
  conversations: [
    {
      name: '佩丽卡',
      messages: [{ id: 1, side: 'other', text: '', isError: true }],
      contextHistory: [
        { side: 'mine', text: '上次说到哪了？' },
        { side: 'other', text: '说到你会记得我。' },
      ],
    },
  ],
}

const LOCAL = {
  cards: [SINGLE_WITH_IMAGE, GROUP_WITH_SPEAKER, CONTEXT_ONLY],
  myGender: 'female',
  promptOverrides: { 伊冯: '你是我改过的伊冯。', 不存在的人: '' },
  sources: ['cardsV2', 'cards'],
}

// ---------------------------------------------------------------------------
// 迷你断言器
// ---------------------------------------------------------------------------

let passed = 0
let failed = 0
const failures = []

async function check(name, fn) {
  try {
    await fn()
    passed += 1
    console.log(`  ok   ${name}`)
  } catch (err) {
    failed += 1
    failures.push({ name, err })
    console.log(`  FAIL ${name}\n       ${err instanceof Error ? err.message : String(err)}`)
  }
}

function section(title) {
  console.log(`\n${title}`)
}

// ---------------------------------------------------------------------------
// A. 转换
// ---------------------------------------------------------------------------

section('[A] 转换：手写老数据 → 导入包')

const converted = buildImportPackage(LOCAL)
const pkg = converted.package
const stats = converted.stats

await check('包版本 / 性别 / 覆盖项', () => {
  assert.equal(pkg.version, 1)
  assert.equal(pkg.myGender, 'female')
  assert.deepEqual(pkg.promptOverrides, { 伊冯: '你是我改过的伊冯。' })
})

await check('卡 / 对话 / 消息条数（isError 与空消息被扣掉）', () => {
  assert.equal(pkg.cards.length, 3, '三张卡都应保留')
  assert.equal(stats.conversations, 3)
  // 2（单聊屏上）+ 4（群聊）+ 2（只剩 AI 记忆的兜底）
  assert.equal(stats.messages, 8)
  assert.equal(stats.screenMessages, 6)
  assert.equal(stats.contextFallback, 2)
  assert.equal(stats.skippedErrors, 2)
  assert.equal(stats.skippedEmpty, 1)
})

await check('side 映射：mine→user / other→assistant', () => {
  const single = pkg.cards[0].conversations[0].messages
  assert.deepEqual(
    single.map((m) => m.role),
    ['user', 'assistant'],
  )
})

await check('topic 行 → system（屏上显示、不进 AI 上下文）', () => {
  const group = pkg.cards[1].conversations[0].messages
  assert.equal(group[2].role, 'system')
  assert.equal(group[2].text, '新话题：今晚吃什么')
})

await check('isError 被丢弃（不在包的任何消息里）', () => {
  const all = pkg.cards.flatMap((c) => c.conversations.flatMap((v) => v.messages))
  assert.equal(
    all.some((m) => m.text.includes('请求失败')),
    false,
  )
  assert.equal(all.length, 6)
})

await check('图片 dataURL 原样保留', () => {
  const img = pkg.cards[0].conversations[0].messages[1]
  assert.equal(img.image, PNG_DATA_URL)
})

await check('群聊元数据（members / myRole / speakMode）跟着卡片走', () => {
  const group = pkg.cards[1]
  assert.deepEqual(group.members, ['伊冯', '佩丽卡'])
  assert.equal(group.myRole, 'admin')
  assert.equal(group.speakMode, 'weighted')
})

await check('speaker / mood 透传', () => {
  const group = pkg.cards[1].conversations[0].messages
  assert.equal(group[0].speakerName, '管理员')
  assert.equal(group[1].speakerName, '佩丽卡')
  assert.equal(group[3].mood, 'sns_emoji_001')
})

await check('屏上消息为空时，contextHistory 原样带上（让后端兜底）', () => {
  const conv = pkg.cards[2].conversations[0]
  assert.equal(conv.messages.length, 0)
  assert.equal(conv.contextHistory.length, 2)
  assert.equal(conv.contextHistory[0].role, 'user')
})

await check('非 dataURL 的图片引用：图丢弃、文字留下 [图片]', () => {
  const local = {
    cards: [
      {
        conversations: [
          {
            name: '伊冯',
            messages: [{ id: 1, side: 'other', text: '', image: 'cards/00/images/00-1.png' }],
          },
        ],
      },
    ],
    myGender: 'male',
    promptOverrides: {},
    sources: [],
  }
  const out = buildImportPackage(local)
  assert.equal(out.package.cards[0].conversations[0].messages[0].text, '[图片]')
  assert.equal(out.package.cards[0].conversations[0].messages[0].image, undefined)
  assert.equal(out.stats.skippedImages, 1)
})

section('[A2] 统计口径（提示条上的数字）')

await check('countLegacyCards 与转换结果一致', () => {
  const counts = countLegacyCards(LOCAL.cards)
  assert.equal(counts.cards, 3)
  assert.equal(counts.conversations, 3)
  assert.equal(counts.messages, 8)
  assert.equal(counts.contextFallback, 2)
  assert.equal(counts.skipped, 3)
})

section('[A3] 分片 + 旧快照的合并（3.2.2 的关键坑）')

await check('旧快照比新分片多出的消息要合并进来，重复的不能翻倍', () => {
  const shard = [
    {
      conversations: [
        {
          name: '陈千语',
          messages: [
            { id: 1, side: 'mine', text: 'a' },
            { id: 2, side: 'other', text: 'b' },
          ],
        },
      ],
    },
  ]
  const legacy = [
    {
      conversations: [
        {
          name: '陈千语',
          messages: [
            { id: 1, side: 'mine', text: 'a' },
            { id: 2, side: 'other', text: 'b' },
            { id: 3, side: 'mine', text: 'c' },
            { id: 4, side: 'other', text: 'd' },
          ],
        },
      ],
    },
  ]
  const merged = mergeLegacyCards(shard, legacy)
  assert.equal(merged.length, 1)
  const texts = merged[0].conversations[0].messages.map((m) => m.text)
  assert.deepEqual(texts, ['a', 'b', 'c', 'd'])
  assert.equal(new Set(merged[0].conversations[0].messages.map((m) => m.id)).size, 4)
})

await check('同 id 不同内容 → 两条都保留（新发一个 id，不许覆盖）', () => {
  const shard = [
    { conversations: [{ name: '陈千语', messages: [{ id: 1, side: 'mine', text: '分片里的第 1 条' }] }] },
  ]
  const legacy = [
    { conversations: [{ name: '陈千语', messages: [{ id: 1, side: 'mine', text: '快照里的第 1 条' }] }] },
  ]
  const merged = mergeLegacyCards(shard, legacy)
  const texts = merged[0].conversations[0].messages.map((m) => m.text)
  assert.equal(texts.length, 2)
  assert.ok(texts.includes('分片里的第 1 条'))
  assert.ok(texts.includes('快照里的第 1 条'))
})

await check('群聊卡按成员集合识别（成员顺序不同也算同一张）', () => {
  const a = [{ members: ['伊冯', '佩丽卡'], conversations: [{ name: '群', messages: [{ id: 1, side: 'mine', text: 'x' }] }] }]
  const b = [{ members: ['佩丽卡', '伊冯'], conversations: [{ name: '群', messages: [{ id: 2, side: 'other', text: 'y' }] }] }]
  const merged = mergeLegacyCards(a, b)
  assert.equal(merged.length, 1)
  assert.equal(merged[0].conversations[0].messages.length, 2)
})

section('[A4] 自动分批（>64MB 的可行路径）')

await check('按上限切片，每片都合法、消息一条不少', () => {
  const image = PNG_DATA_URL + 'A'.repeat(30_000)
  const big = {
    cards: [
      {
        conversations: [
          {
            name: '大图角色',
            messages: Array.from({ length: 12 }, (_, i) => ({
              id: i + 1,
              side: i % 2 === 0 ? 'mine' : 'other',
              text: `第 ${i + 1} 条`,
              image: image,
            })),
          },
        ],
      },
    ],
    myGender: 'male',
    promptOverrides: { 大图角色: '自定义' },
    sources: [],
  }
  const built = buildImportPackage(big).package
  const maxBytes = 120_000 // 约 120KB：原包 ~500KB，必然要分
  const pieces = splitImportPackage(built, big, maxBytes)
  assert.ok(pieces.length > 1, `应该分批，实际 ${pieces.length}`)
  let totalMessages = 0
  for (const piece of pieces) {
    const bytes = Buffer.byteLength(JSON.stringify(piece), 'utf8')
    assert.ok(bytes <= maxBytes, `单批 ${bytes} 字节超过上限 ${maxBytes}`)
    for (const card of piece.cards) {
      for (const conv of card.conversations) totalMessages += conv.messages.length
    }
  }
  assert.equal(totalMessages, 12)
  // 自定义提示词只在第一批：重复 upsert 无害，但没必要每批都带
  assert.equal(pieces[0].promptOverrides?.['大图角色'], '自定义')
  assert.equal(pieces.slice(1).some((p) => p.promptOverrides), false)
})

// ---------------------------------------------------------------------------
// B. HTTP：假后端 + runMigration
// ---------------------------------------------------------------------------

/** 内存版 localStorage（核心只要求 getItem / setItem） */
function memoryStorage() {
  const map = new Map()
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => {
      map.set(key, String(value))
    },
    _map: map,
  }
}

/** 从 multipart body 里抠出 JSON 文件内容（够用即可，不追求通用解析器） */
function extractJsonFile(body) {
  const start = body.indexOf('{')
  if (start < 0) return null
  // 从第一个 { 开始做括号配对，忽略字符串里的括号（图片 base64 里没有括号）
  let depth = 0
  let inString = false
  let escaped = false
  for (let i = start; i < body.length; i += 1) {
    const ch = body[i]
    if (inString) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') inString = true
    else if (ch === '{') depth += 1
    else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        try {
          return JSON.parse(body.slice(start, i + 1))
        } catch {
          return null
        }
      }
    }
  }
  return null
}

function startFakeServer(state) {
  const requests = []
  const server = http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/v1/auth/device') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ device_id: 'dev-test', token: 'token-test' }))
      return
    }
    if (req.method === 'POST' && req.url === '/v1/imports') {
      const chunks = []
      req.on('data', (chunk) => chunks.push(chunk))
      req.on('end', () => {
        const raw = Buffer.concat(chunks)
        const body = raw.toString('utf8')
        const packageJson = extractJsonFile(body)
        requests.push({
          headers: req.headers,
          body,
          packageJson,
          bytes: raw.length,
        })

        if (state.failStatus) {
          const status = state.failStatus
          res.writeHead(status, { 'Content-Type': 'application/json' })
          res.end(JSON.stringify({ error: { code: `http_${status}`, message: 'fake failure' } }))
          return
        }

        let cards = 0
        let conversations = 0
        let messages = 0
        let images = 0
        for (const card of packageJson?.cards ?? []) {
          cards += 1
          for (const conv of card.conversations ?? []) {
            conversations += 1
            const messagesList = conv.messages ?? []
            const context = conv.contextHistory ?? []
            // 照后端 normalize._conversation 的规则：屏上消息为空时用 contextHistory 兜底
            messages += messagesList.length > 0 ? messagesList.length : context.length
            images += messagesList.filter((m) => m.image).length
          }
        }
        const id = `imp-${requests.length}`
        state.batches.push(id)
        res.writeHead(201, { 'Content-Type': 'application/json' })
        res.end(
          JSON.stringify({
            id,
            duplicate: false,
            source: 'json',
            mode: 'merge',
            package_version: 1,
            cards,
            conversations,
            messages,
            images,
            skipped: 0,
            notes: [],
            created_at: '2026-11-18T00:00:00+00:00',
            revoked_at: null,
          }),
        )
      })
      return
    }
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ error: { code: 'not_found', message: 'no route' } }))
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      resolve({ server, port: address.port, requests })
    })
  })
}

section('[B] HTTP：假后端 + runMigration')

const state = { failStatus: 0, batches: [] }
const { server, port, requests } = await startFakeServer(state)
const importUrl = `http://127.0.0.1:${port}/v1/imports`
const storage = memoryStorage()

let outcome = null
try {
  outcome = await runMigration({
    readLocal: async () => LOCAL,
    ensureToken: async () => 'token-test',
    importUrl,
    storage,
    now: () => 1_700_000_000_000,
  })
} finally {
  // 断言写在下面；server 最后统一关
}

await check('上传成功返回 201，计数与本地一致', () => {
  assert.ok(outcome && !outcome.skipped)
  assert.equal(outcome.counts.conversations, 3)
  assert.equal(outcome.counts.messages, 8) // 服务端把兜底也算成消息
  assert.equal(outcome.local.messages, 8)
  assert.equal(outcome.pieces, 1)
})

await check('请求带 Bearer / multipart / mode=merge', () => {
  assert.equal(requests.length, 1)
  const req = requests[0]
  assert.equal(req.headers.authorization, 'Bearer token-test')
  assert.match(String(req.headers['content-type']), /multipart\/form-data/)
  assert.match(req.body, /name="file"/)
  assert.match(req.body, /name="mode"/)
  assert.match(req.body, /merge/)
  assert.equal(req.packageJson?.version, 1)
  assert.equal(req.packageJson?.myGender, 'female')
})

await check('成功写迁移标记（batchId / at / counts）', () => {
  const raw = storage.getItem(MIGRATE_MARKER_KEY)
  assert.ok(raw, '标记应该写入')
  const marker = JSON.parse(raw)
  assert.equal(marker.batchId, 'imp-1')
  assert.equal(marker.at, 1_700_000_000_000)
  assert.equal(marker.counts.messages, 8)
  assert.deepEqual(marker.batchIds, ['imp-1'])
})

await check('第二次运行不再上传（不重复导）', async () => {
  const before = requests.length
  const again = await runMigration({
    readLocal: async () => LOCAL,
    ensureToken: async () => 'token-test',
    importUrl,
    storage,
    now: () => 1_700_000_001_000,
  })
  assert.equal(again.skipped, true)
  assert.equal(requests.length, before)
})

section('[B2] 失败路径')

await check('413 → too_large，且不写标记', () => {
  state.failStatus = 413
  const fresh = memoryStorage()
  return runMigration({
    readLocal: async () => LOCAL,
    ensureToken: async () => 'token-test',
    importUrl,
    storage: fresh,
  }).then(
    () => {
      throw new Error('应该抛出 413')
    },
    (err) => {
      assert.ok(err instanceof LegacyMigrationError)
      assert.equal(err.kind, 'too_large')
      assert.equal(fresh.getItem(MIGRATE_MARKER_KEY), null, '失败绝不能写标记')
      assert.match(describeMigrationError(err), /导出数据/)
      assert.match(describeMigrationError(err), /导入数据/)
    },
  )
})
state.failStatus = 0

section('[B3] 自动分批：一次迁移拆成多批，计数累加')

// 用大图样本 + 很小的 maxBytes，让核心真的分批上传
const bigImage = PNG_DATA_URL + 'B'.repeat(20_000)
const bigLocal = {
  cards: [
    {
      conversations: [
        {
          name: '大图角色',
          messages: Array.from({ length: 6 }, (_, i) => ({
            id: i + 1,
            side: i % 2 === 0 ? 'mine' : 'other',
            text: `第 ${i + 1} 条`,
            image: bigImage,
          })),
        },
      ],
    },
  ],
  myGender: 'male',
  promptOverrides: {},
  sources: [],
}
const batchStorage = memoryStorage()
const beforeBatches = requests.length
const batchOutcome = await runMigration({
  readLocal: async () => bigLocal,
  ensureToken: async () => 'token-test',
  importUrl,
  storage: batchStorage,
  maxBytes: 60_000,
  now: () => 1_700_000_002_000,
})

await check('分多批上传，消息总数不丢', () => {
  const uploaded = requests.slice(beforeBatches)
  assert.ok(uploaded.length > 1, `应该分多批，实际 ${uploaded.length}`)
  assert.equal(batchOutcome.pieces, uploaded.length)
  const sum = uploaded.reduce(
    (n, req) =>
      n +
      (req.packageJson?.cards ?? []).reduce(
        (m, card) => m + card.conversations.reduce((k, conv) => k + conv.messages.length, 0),
        0,
      ),
    0,
  )
  assert.equal(sum, 6)
  assert.equal(batchOutcome.counts.messages, 6)
  assert.equal(batchOutcome.counts.conversations, uploaded.length)
  assert.equal(batchOutcome.marker.batchIds.length, uploaded.length)
})

await check('分批摘要会说"分 N 批迁移"', () => {
  assert.match(describeMigration(batchOutcome), /分 \d+ 批迁移/)
})

// ---------------------------------------------------------------------------
// 收尾
// ---------------------------------------------------------------------------

await new Promise((resolve) => server.close(resolve))

console.log(`\n${passed}/${passed + failed} passed`)
if (failed > 0) {
  for (const item of failures) {
    console.error(`\n失败：${item.name}`)
    console.error(item.err)
  }
  process.exitCode = 1
}
