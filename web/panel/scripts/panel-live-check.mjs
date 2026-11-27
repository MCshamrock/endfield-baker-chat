#!/usr/bin/env node
/**
 * scripts/panel-live-check.mjs —— 拿**真后端**把面板用到的 8 个接口打一遍。
 *
 * 和 `panel-smoke.mjs` 的分工：
 *   · smoke：假后端，测"接口层的行为"（token、401/403、分页参数、错误体），随时能跑；
 *   · live ：真后端，测"契约有没有变"（字段名、形状、写操作能不能落盘），
 *            部署前/切后端后各跑一次，几秒钟出结论。
 *
 * 安全约束（很重要，这是个能改线上数据的脚本）：
 *   1. **只做幂等或不留痕的写**：用户/配置/密钥都写回"本来就是的值"，只改 updated_at；
 *   2. 干员权重做一次 +1 再改回来（用 finally 保证恢复），用来验证写真的落盘；
 *   3. 绝不改角色、绝不改别人的账号、绝不动 provider key 的启用状态。
 *
 * 用法：
 *   BAKER_PANEL_USER=panel_admin BAKER_PANEL_PASS=*** node scripts/panel-live-check.mjs
 *   可选：--base http://127.0.0.1:5660   --conversation <public_id>
 */

import { createApiClient, ApiError, memoryTokenStore } from '../src/lib/api.ts'

const argv = process.argv.slice(2)
function arg(name, fallback = '') {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback
}

const baseUrl = arg('base', process.env.BAKER_PANEL_BASE || 'http://127.0.0.1:5660')
const username = process.env.BAKER_PANEL_USER || arg('user', '')
const password = process.env.BAKER_PANEL_PASS || arg('pass', '')
const conversationId = arg('conversation', process.env.BAKER_PANEL_CONVERSATION || '')

if (!username || !password) {
  console.error('缺少账号：用 BAKER_PANEL_USER / BAKER_PANEL_PASS 环境变量给，或者 --user/--pass')
  process.exit(2)
}

const results = []
async function step(name, fn) {
  try {
    const note = await fn()
    results.push({ name, ok: true, note: note || '' })
    console.log(`PASS  ${name}${note ? `  （${note}）` : ''}`)
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    results.push({ name, ok: false, note: detail })
    console.log(`FAIL  ${name}\n      ${detail}`)
  }
}

function need(cond, message) {
  if (!cond) throw new Error(message)
}

function num(value, what) {
  need(typeof value === 'number' && Number.isFinite(value), `${what} 不是数字：${JSON.stringify(value)}`)
  return value
}

const client = createApiClient({
  baseUrl,
  tokenStore: memoryTokenStore(),
  onUnauthorized: () => console.log('      [401] 触发了"回登录页"'),
})

console.log(`真后端：${baseUrl}\n账号：${username}\n`)

let charsBefore = null

async function main() {
  await step('① 设备注册 → 拿 token', async () => {
    const token = await client.ensureDevice()
    need(token.length > 10, 'token 太短')
    return `token 长度 ${token.length}`
  })

  await step('② 登录（POST /v1/auth/login）', async () => {
    const user = await client.login(username, password)
    need(user.username === username, `登录回来的用户名不对：${user.username}`)
    return `${user.username} / ${user.display_name || '(无显示名)'}`
  })

  await step('③ 权限探针 + overview 字段齐（GET /v1/admin/overview）', async () => {
    const data = await client.overview()
    for (const f of ['users', 'devices', 'conversations', 'messages', 'characters']) num(data.counts[f], `counts.${f}`)
    for (const f of ['total', 'enabled', 'cooldown', 'disabled']) num(data.keys[f], `keys.${f}`)
    for (const f of ['requests', 'prompt_tokens', 'completion_tokens', 'cached_tokens', 'avg_ms']) num(data.today[f], `today.${f}`)
    need(Array.isArray(data.days), 'days 不是数组')
    need(!Number.isNaN(Date.parse(data.now)), `now 不合法：${data.now}`)
    return `用户 ${data.counts.users} · 干员 ${data.counts.characters} · key ${data.keys.enabled}/${data.keys.total} · 今日 ${data.today.requests} 次`
  })

  await step('④ 用户列表 + 分页（GET /v1/admin/users）', async () => {
    const page = await client.listUsers({ limit: 3, offset: 0 })
    need(Array.isArray(page.items), 'items 不是数组')
    need(page.items.length <= 3, `limit=3 却回了 ${page.items.length} 行`)
    num(page.total, 'total')
    for (const row of page.items) {
      need(typeof row.id === 'string' && typeof row.username === 'string', 'row 缺 id/username')
      need(['active', 'disabled'].includes(row.status), `row.status 不认识：${row.status}`)
      need(['user', 'admin'].includes(row.role), `row.role 不认识：${row.role}`)
    }
    const second = await client.listUsers({ q: '', limit: 2, offset: 2 })
    need(second.offset === 2, `offset 没回显：${second.offset}`)
    return `total ${page.total}，本页 ${page.items.length} 行`
  })

  await step('⑤ 用户 PATCH 写回原值（GET → PATCH → 再 GET 一致）', async () => {
    const page = await client.listUsers({ q: username, limit: 5 })
    const me = page.items.find((u) => u.username === username)
    need(me, `列表里找不到 ${username}`)
    const out = await client.patchUser(me.id, { display_name: me.display_name })
    need(out.display_name === me.display_name, `回显不一致：${out.display_name}`)
    return `${username} 显示名「${me.display_name || '(空)'}」未被改动`
  })

  await step('⑥ 不能把自己降权（PATCH role=user 应 400）', async () => {
    const page = await client.listUsers({ q: username, limit: 5 })
    const me = page.items.find((u) => u.username === username)
    need(me, `列表里找不到 ${username}`)
    try {
      await client.patchUser(me.id, { role: 'user' })
    } catch (err) {
      need(err instanceof ApiError, '抛的不是 ApiError')
      need(err.status === 400, `状态码应为 400，实际 ${err.status}`)
      need(err.code === 'cannot_demote_self', `错误码应为 cannot_demote_self，实际 ${err.code}`)
      return `400 ${err.code} · ${err.message}`
    }
    throw new Error('后端居然允许自己降权了')
  })

  await step('⑦ 干员列表（含去重前的原始行数）', async () => {
    const out = await client.listCharacters()
    charsBefore = out.items
    need(out.items.length > 0, '干员列表是空的')
    const slugs = new Set(out.items.map((c) => c.id))
    for (const c of out.items) {
      num(c.active_weight, `${c.id}.active_weight`)
      num(c.prompt_ver, `${c.id}.prompt_ver`)
      need(typeof c.enabled === 'boolean', `${c.id}.enabled 不是布尔`)
    }
    return `${out.items.length} 行 / ${slugs.size} 个干员`
  })

  await step('⑧ 干员 PATCH：权重 +1 再改回（验证写真的落盘）', async () => {
    const target = charsBefore[0]
    const original = target.active_weight
    const bumped = original >= 100 ? original - 1 : original + 1
    try {
      const up = await client.patchCharacter(target.id, { active_weight: bumped })
      need(up.active_weight === bumped, `权重回显不对：${up.active_weight} ≠ ${bumped}`)
      const back = await client.patchCharacter(target.id, { active_weight: original })
      need(back.active_weight === original, `恢复失败：${back.active_weight} ≠ ${original}`)
      return `${target.name} ${original} → ${bumped} → ${original}`
    } finally {
      // 兜底：万一上面哪一步炸了，也把权重写回去（避免把测试值留在库里）
      try {
        await client.patchCharacter(target.id, { active_weight: original })
      } catch {
        /* 已经恢复过就算了 */
      }
    }
  })

  await step('⑨ 配置：读 → 原值写回（PUT 会清缓存，写回等于暖一次）', async () => {
    const out = await client.listConfig()
    need(out.items.length > 0, '配置表是空的')
    const first = out.items[0]
    need(typeof first.key === 'string' && typeof first.value === 'string', '配置项缺 key/value')
    const written = await client.putConfig(first.key, first.value)
    need(written.key === first.key, `key 回显不对：${written.key}`)
    need(written.value === first.value, 'value 回显不对（可能被截断）')
    return `${out.items.length} 条，回写 ${first.key}（${first.value.length} 字）`
  })

  await step('⑩ 密钥池：只回状态，且没有密钥字段', async () => {
    const out = await client.listKeys()
    const allowed = new Set(['id', 'label', 'enabled', 'cooling', 'cooldown_until', 'fail_count', 'rpm', 'base_url', 'model', 'last_used_at'])
    for (const key of out.items) {
      for (const field of Object.keys(key)) {
        need(allowed.has(field), `出现了不该有的字段：${field}`)
        need(!/key|secret|token|enc/i.test(field), `字段名像密钥：${field}`)
      }
    }
    const stats = client.keyStatsOf(out.items)
    return `${out.items.length} 把 · 启用 ${stats.enabled} · 冷却 ${stats.cooldown} · 禁用 ${stats.disabled}`
  })

  await step('⑪ 密钥：把第一把的启用状态写回原值', async () => {
    const out = await client.listKeys()
    const first = out.items[0]
    const echoed = await client.patchKey(first.id, first.enabled)
    need(echoed.enabled === first.enabled, '启用状态回显不一致')
    return `${first.label} 保持 ${first.enabled ? '启用' : '禁用'}`
  })

  await step('⑫ 用量 7 / 30 / 90', async () => {
    const notes = []
    for (const days of [7, 30, 90]) {
      const out = await client.usage(days)
      need(out.days === days, `days 回显不对：${out.days} ≠ ${days}`)
      for (const row of out.items) {
        need(/^\d{4}-\d{2}-\d{2}$/.test(row.date), `日期格式不对：${row.date}`)
        need(typeof row.requests === 'number', 'requests 不是数字')
      }
      notes.push(`${days}天 ${out.items.length} 天有量`)
    }
    return notes.join(' · ')
  })

  await step('⑬ 思考链（需要一个带 reasoning 的会话 id）', async () => {
    if (!conversationId) return '跳过：没给 --conversation（没有会话列表接口，id 得从库里拿）'
    const out = await client.reasoning(conversationId, 5)
    need(out.conversation_id === conversationId, '会话 id 回显不对')
    need(Array.isArray(out.items), 'items 不是数组')
    for (const item of out.items) {
      num(item.seq, 'seq')
      need(typeof item.reasoning === 'string', 'reasoning 不是字符串')
    }
    return `${out.items.length} 条带思考的消息`
  })

  const passed = results.filter((r) => r.ok).length
  console.log(`\n${passed}/${results.length} 通过`)
  if (passed !== results.length) process.exitCode = 1
}

main().catch((err) => {
  console.error('联调脚本自己挂了：', err)
  process.exitCode = 1
})
