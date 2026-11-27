// =============================================================================
// wall-e2e.mjs —— 「有记录就必须先登录」那道墙的浏览器验收
// -----------------------------------------------------------------------------
// 真浏览器（Edge headless + CDP）+ 真本地后端（5660，经 vite 5173 代理）。
//
// 五条断言：
//   1. 全新浏览器（没有任何记录）→ **不出现**墙，直接可用
//   2. 伪造一个"有 1 段对话 3 条消息"的设备 → **出现**墙，且文案带数量
//   3. 墙上注册 → 墙消失
//   4. 注册后这个 token 名下的记录还在（adopt 生效，不是重新建了一份）
//   5. 已登录的人再刷新 → 不出现墙（不会把自己锁在外面）
//
// 运行：node _runtime/login-wall/wall-e2e.mjs
// =============================================================================
import { launch, connect, sleep, waitFor, newProfileDir, wipeDir } from '../audit-front/harness.mjs'

const PORT = 9334
const BASE = process.env.WALL_BASE || 'http://localhost:5173/'
const API = process.env.WALL_API || 'http://127.0.0.1:5660/v1'

let pass = 0
let fail = 0
function check(ok, name, detail = '') {
  if (ok) { pass++; console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`) }
  else { fail++; console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`) }
  return !!ok
}

async function api(method, path, body, token) {
  const res = await fetch(API + path, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  let json = {}
  try { json = text ? JSON.parse(text) : {} } catch { json = { raw: text } }
  return { status: res.status, json }
}

/** 造一台"已经有聊天记录"的设备：设备 + 1 段会话 + 3 条消息 */
async function seedDeviceWithHistory() {
  const d = await api('POST', '/auth/device', { platform: 'web', app_ver: '3.2.2' })
  if (d.status !== 201) throw new Error('建设备失败 ' + JSON.stringify(d.json))
  const token = d.json.token
  const c = await api('POST', '/conversations', { character_id: 'perlica', mode: 'single' }, token)
  if (c.status !== 201) throw new Error('建会话失败 ' + JSON.stringify(c.json))
  const convId = c.json.id
  for (let i = 0; i < 3; i++) {
    const m = await api('POST', `/conversations/${convId}/messages`, {
      client_msg_id: `wall-${Date.now()}-${i}`,
      text: `第 ${i + 1} 条测试消息`,
      generate: false,
    }, token)
    if (m.status !== 200) throw new Error('发消息失败 ' + JSON.stringify(m.json))
  }
  const own = await api('GET', '/auth/owner-data', undefined, token)
  console.log(`  种子：会话 1 段 / 消息 ${own.json.messages} 条（owner-data 自检 ${own.json.conversations}/${own.json.messages}）`)
  return token
}

/** 读墙的状态（不依赖组件内部，只看 DOM） */
const WALL_DOM = `(() => {
  const el = document.querySelector('.lw');
  if (!el) return { present: false };
  const txt = (el.innerText || '').replace(/\\s+/g, ' ');
  return {
    present: true,
    visible: el.getClientRects().length > 0,
    title: (document.querySelector('.lw__title')?.innerText || '').trim(),
    count: (document.querySelector('.lw__count')?.innerText || '').trim(),
    tabs: [...document.querySelectorAll('.lw__tab')].map(t => t.innerText.trim()),
    submit: (document.querySelector('.lw__submit')?.innerText || '').trim(),
    hasSnippet: txt.slice(0, 400),
  };
})()`

async function fill(s, selector, value) {
  await s.focusAt(selector)
  await s.eval(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); el.select?.(); return true })()`)
  await s.key('Backspace')
  await s.type(value)
  await sleep(120)
}

wipeDir(await newProfileDir('wall'))
const browser = await launch({ port: PORT, profileDir: 'F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/profiles/wall', width: 1440, height: 900 })
const s = await connect({ port: PORT })

try {
  // ---------------------------------------------------------------- 1. 全新浏览器
  await s.goto(BASE)
  await sleep(3200)
  let dom = await s.eval(WALL_DOM)
  check(!dom.present, '全新浏览器不出墙', dom.present ? JSON.stringify(dom) : '无 .lw')
  await s.screenshot('F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/01-fresh-no-wall.png')

  // ---------------------------------------------------------------- 2. 有记录的设备
  const token = await seedDeviceWithHistory()
  await s.eval(`localStorage.setItem('baker_device_token', ${JSON.stringify(token)}); true`)
  await s.goto(BASE)
  await sleep(3500)

  dom = await s.eval(WALL_DOM)
  check(dom.present && dom.visible, '有记录的设备出现墙')
  check((dom.title || '').includes('还没挂到账号上'), '标题是"还没挂到账号上"', dom.title)
  check((dom.count || '').includes('1 段对话'), '文案里有数量', dom.count)
  check((dom.tabs || []).join('/') === '登录/注册', '两个页签都在', (dom.tabs || []).join('/'))
  await s.screenshot('F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/02-wall-shown.png')

  // 墙必须真的挡住界面：底下的输入框点不到（拿坐标判断被覆盖）
  const covered = await s.eval(`(() => {
    const el = document.elementFromPoint(720, 450);
    return el ? el.className || el.tagName : null;
  })()`)
  check(String(covered).includes('lw'), '墙盖在界面上（命中 .lw）', String(covered))

  // ---------------------------------------------------------------- 3. 注册
  const uname = 'wall' + Date.now().toString().slice(-8)
  const pw = 'wallpass12345'
  await fill(s, '.lw__field:nth-child(1) input', uname)
  await fill(s, '.lw__field:nth-child(2) input', pw)
  await fill(s, '.lw__field:nth-child(3) input', pw)
  await s.clickSelector('.lw__submit')

  await waitFor(async () => {
    const d = await s.eval(WALL_DOM)
    return !d.present
  }, { timeout: 15000, label: 'wall-gone' }).catch(() => {})

  dom = await s.eval(WALL_DOM)
  check(!dom.present, '注册成功后墙消失', dom.present ? JSON.stringify(dom.title) : '')
  await sleep(1200)
  await s.screenshot('F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/03-after-register.png')

  // ---------------------------------------------------------------- 4. 记录还在
  const own = await api('GET', '/auth/owner-data', undefined, token)
  check(own.json.conversations >= 1, '注册后记录仍在（adopt 生效）', `${own.json.conversations} 段 / ${own.json.messages} 条`)
  const me = await api('GET', '/auth/me', undefined, token)
  check(me.json.kind === 'user', 'token 现在是账号身份', me.json.user?.username || me.json.kind)

  // ---------------------------------------------------------------- 5. 已登录刷新不再出墙
  await s.goto(BASE)
  await sleep(3000)
  dom = await s.eval(WALL_DOM)
  check(!dom.present, '已登录刷新不再出墙')

  // 控制台
  const errs = s.consoleErrors.filter((t) => !/favicon|net::ERR_/i.test(t))
  check(errs.length === 0, '控制台无报错', errs.slice(0, 3).join(' | '))
} catch (err) {
  fail++
  console.log('  FAIL  异常：', err?.message || err)
  try { await s.screenshot('F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/99-error.png') } catch {}
} finally {
  s.close()
  await browser.close()
}

console.log(`\n合计：${pass} PASS / ${fail} FAIL`)
process.exit(fail === 0 ? 0 : 1)
