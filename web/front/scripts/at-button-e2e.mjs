#!/usr/bin/env node
// at-button-e2e.mjs —— 群聊「@」按钮（站长手绘图）真页面回归
//
// 为什么单独一条：这个按钮的图标是**一张图**，不是字体字形，它会不会被 50% 圆角切掉、
// 会不会在按钮里缩成一小坨、线上的 png 有没有真的发出去，只有真页面能证明。
//
// 覆盖（桌面 1280×900 与移动 430×900 各跑一遍）：
//   1. 建一个 2 人群聊，进聊天
//   2. 按钮里是 <img>、图片加载成功（naturalWidth>0）、四周留 1px、圆角 50%
//   3. 点一下弹成员列表、选人把「@名字 」插进输入框、浮层收起（移动端曾会弹回来）
//
// 依赖（和 regen-e2e-cdp.mjs 同一套）：
//   - 静态站：`python -m http.server 15800 --directory dist`
//   - 无头 Edge：`msedge --headless=new --remote-debugging-port=9333`
// 环境变量：
//   BAKER_PAGE  页面地址（默认 http://127.0.0.1:15800/，也可指 https://chat.peilika.beer/）
//   BAKER_CDP   CDP 地址（默认 http://127.0.0.1:9333）
//   BAKER_W / BAKER_H / BAKER_MOBILE   视口（移动端用 BAKER_W=430 BAKER_MOBILE=1）
//   BAKER_OUT   截图输出（默认 ./at-button.png）
import { connect, sleep } from './cdp-lite.mjs'
import { writeFileSync } from 'node:fs'

const PAGE = process.env.BAKER_PAGE || 'http://127.0.0.1:15800/'
const OUT = process.env.BAKER_OUT || './at-button.png'
const FULL = OUT.replace(/\.png$/, '_full.png')

let pass = 0
let fail = 0
const check = (c, n, d = '') => { c ? pass++ : fail++; console.log(`  ${c ? 'PASS' : 'FAIL'}  ${n}${d ? '  · ' + d : ''}`) }

const cdp = await connect()
const VW = Number(process.env.BAKER_W || 1280), VH = Number(process.env.BAKER_H || 900), VMOBILE = process.env.BAKER_MOBILE === '1'
await cdp.send('Emulation.setDeviceMetricsOverride', { width: VW, height: VH, deviceScaleFactor: 2, mobile: VMOBILE })
await cdp.send('Page.navigate', { url: PAGE })
await sleep(2500)

async function clickText(text) {
  return cdp.eval(`(() => {
    const want = ${JSON.stringify(text)}
    const all = Array.from(document.querySelectorAll('button, [role="button"], li, .menu-item, span, div, a, p'))
    const hits = all.filter((el) => (el.textContent || '').trim() === want)
    if (!hits.length) return false
    let el = hits[0]
    for (const h of hits) { if (h.querySelectorAll('*').length < el.querySelectorAll('*').length) el = h }
    const t = el.closest('button, [role="button"], li, .menu-item, a') || el
    t.scrollIntoView({ block: 'center' }); t.click(); return true
  })()`)
}

await cdp.eval(`(async () => {
  localStorage.clear()
  await new Promise((r) => { const q = indexedDB.deleteDatabase('endfield-baker'); q.onsuccess = q.onerror = q.onblocked = r })
  return true
})()`)
await cdp.send('Page.reload')
await sleep(2800)
for (const t of ['稍后再说', '跳过', '不开启', '确定', '不再提醒', '好', '知道了']) { if (await clickText(t)) await sleep(600) }

await cdp.eval(`(() => { const b = document.querySelector('[title="群聊"], [aria-label="群聊"]'); if (b) b.click(); return !!b })()`)
await sleep(800)
await cdp.eval(`(() => { const b = document.querySelector('.mode-switch-host__side .mode-switch-host__action'); if (b) b.click(); return !!b })()`)
await sleep(800)
const picked = await cdp.eval(`(() => {
  const it = Array.from(document.querySelectorAll('.gc__item'))
  if (it.length < 2) return ''
  it[0].click(); it[1].click()
  return it.slice(0, 2).map((e) => e.textContent.trim()).join(' / ')
})()`)
await sleep(300)
await clickText('创建群聊')
await sleep(1200)
for (const t of ['跳过', '不开启', '稍后再说', '确定', '知道了']) { if (await clickText(t)) await sleep(500) }
// 移动端建完群停在卡片列表，要点一下群卡才进聊天
if (!(await cdp.eval(`!!document.querySelector('.chat-input')`))) {
  await cdp.eval(`(() => {
    const c = document.querySelector('.card.is-selected') || document.querySelector('.group-unit.is-active') || document.querySelector('[class*="group"]')
    if (c) c.click(); return !!c
  })()`)
  await sleep(1200)
  for (const t of ['跳过', '不开启', '稍后再说', '确定']) { if (await clickText(t)) await sleep(500) }
}
check(await cdp.eval(`!!document.querySelector('.chat-input')`), '群聊建好、输入面板出现', picked)

// ---- 图标本体 ----
const info = await cdp.eval(`(() => {
  const img = document.querySelector('.chat-input .is-mention-trigger img')
  if (!img) return null
  const r = img.getBoundingClientRect()
  const b = img.closest('button').getBoundingClientRect()
  const cs = getComputedStyle(img)
  return JSON.stringify({
    iw: +r.width.toFixed(2), ih: +r.height.toFixed(2), bw: +b.width.toFixed(2), bh: +b.height.toFixed(2),
    src: img.getAttribute('src'), loaded: img.complete && img.naturalWidth > 0,
    nw: img.naturalWidth, nh: img.naturalHeight,
    radius: cs.borderRadius, fit: cs.objectFit, pos: cs.position,
  })
})()`)
check(!!info, '按钮里是 <img>（不再排字体 @）', info || '')
const d = info ? JSON.parse(info) : null
if (d) {
  check(d.loaded, '图片加载成功', `${d.nw}×${d.nh} 原图 → 显示 ${d.iw}×${d.ih}`)
  check(d.src.includes('icon_sns_chat_at'), '用的是手绘图素材', d.src.split('/').pop())
  // 与客户端一致：四周各留 1px（页面有 0.667 缩放，所以按"内缩 0~2px"判）
  const inset = d.bw - d.iw
  check(inset > 0 && inset <= 2.1 && Math.abs(d.iw - d.ih) < 0.6, '铺满整颗按钮、四周留 1px 边', `按钮 ${d.bw} × 图标 ${d.iw}（内缩 ${inset.toFixed(2)}）`)
  check(d.radius === '50%', '圆角裁成圆（不露白方块四角）', d.radius)
  check(d.pos === 'absolute' && d.fit === 'contain', '定位/缩放符合预期', `${d.pos} / ${d.fit}`)
}

// ---- 交互 ----
await cdp.eval(`document.querySelector('.chat-input .is-mention-trigger').click()`)
await sleep(500)
const pop = await cdp.eval(`(() => {
  const p = document.querySelector('.chat-input__mention')
  if (!p) return null
  const names = Array.from(p.querySelectorAll('.chat-input__mention-name')).map((e) => e.textContent.trim())
  const r = p.getBoundingClientRect()
  return JSON.stringify({ names, visible: r.width > 0 && r.height > 0 })
})()`)
check(!!pop, '点按钮弹出成员列表', pop || '')
const p = pop ? JSON.parse(pop) : null
check(!!(p && p.visible && p.names.length >= 2), '列表里有全部成员', p ? p.names.join(',') : '')
if (p?.names?.length) {
  await cdp.eval(`document.querySelectorAll('.chat-input__mention-item')[0].click()`)
  await sleep(400)
  const text = await cdp.eval(`(() => {
    const el = document.querySelector('.chat-input [contenteditable="true"]') || document.querySelector('.chat-input textarea')
    return el ? (el.innerText || el.value || '') : ''
  })()`)
  check(text.includes('@' + p.names[0]), '选人后「@名字 」插进输入框', JSON.stringify(text))
  check(!(await cdp.eval(`!!document.querySelector('.chat-input__mention')`)), '插入后浮层收起')
}

// ---- 截图 ----
const rect = JSON.parse(await cdp.eval(`(() => { const r = document.querySelector('.chat-input').getBoundingClientRect()
  return JSON.stringify({ x: r.x, y: r.y, w: r.width, h: r.height }) })()`))
const full = await cdp.send('Page.captureScreenshot', { format: 'png' })
writeFileSync(FULL, Buffer.from(full.data, 'base64'))
const crop = await cdp.send('Page.captureScreenshot', {
  format: 'png',
  clip: { x: Math.max(0, rect.x - 8), y: Math.max(0, rect.y - 60), width: rect.w + 16, height: rect.h + 68, scale: 2 },
  captureBeyondViewport: true,
})
writeFileSync(OUT, Buffer.from(crop.data, 'base64'))

console.log(`\n${pass} PASS / ${fail} FAIL`)
console.log('截图:', OUT)
process.exit(fail ? 1 : 0)
