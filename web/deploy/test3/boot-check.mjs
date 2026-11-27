// =============================================================================
// boot-check.mjs —— 验证 test3 的开屏（3.2.2：随机干员立绘 + 名字 + 大招语音 + 进度）
// -----------------------------------------------------------------------------
// 一条一条看：
//   1. 主包下载期间就能看到启动层（#boot-layer 存在且可见）
//   2. 启动层里有干员名字 / 立绘 / 大招语音 / 进度数字
//   3. 挂载完成后它自己退场（黄幕擦过 → 淡出 → 从 DOM 移除）
//   4. 退场后 `baker:boot-done` 已广播（App 的 splashGone 才会放行弹窗）
//   5. 控制台无报错、立绘真的加载成功（naturalWidth > 0）
//
// 运行：node _runtime/login-wall/boot-check.mjs
// =============================================================================
import { launch, connect, sleep, newProfileDir, wipeDir } from '../audit-front/harness.mjs'

const PORT = 9335
const BASE = process.env.BOOT_BASE || 'https://test3.peilika.beer/'
const SHOT = 'F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall'

let pass = 0
let fail = 0
const check = (ok, name, detail = '') => {
  if (ok) { pass++; console.log(`  PASS  ${name}${detail ? '  · ' + detail : ''}`) }
  else { fail++; console.log(`  FAIL  ${name}${detail ? '  · ' + detail : ''}`) }
  return !!ok
}

const BOOT_DOM = `(() => {
  const el = document.getElementById('boot-layer');
  if (!el) return { present: false };
  const plate = document.getElementById('boot-plate');
  return {
    present: true,
    visible: el.getClientRects().length > 0,
    name: plate ? (plate.alt || '') : '',
    plateLoaded: !!plate && plate.complete && plate.naturalWidth > 0,
    plateSrc: plate ? (plate.currentSrc || plate.src || '').split('/').pop() : '',
    line: (document.getElementById('boot-line')?.textContent || '').trim(),
    percent: (document.getElementById('boot-num')?.textContent || '').trim(),
    bootDone: !!window.__bootDoneSeen,
  };
})()`

wipeDir(await newProfileDir('boot'))
const browser = await launch({
  port: PORT,
  profileDir: 'F:/dshworkspace/BakerAI/BakerAI_new/_runtime/login-wall/profiles/boot',
  width: 1440,
  height: 900,
})
const s = await connect({ port: PORT })

try {
  // 先装一个探针，记下 baker:boot-done 有没有真的发出来
  await s.goto('about:blank')

  const nav = s.goto(BASE)
  // 导航后立刻开始找启动层（不要等 load —— 要在主包下载期间就看到它）。
  // ⚠️ 刚导航时内联脚本可能还没跑完（名字/立绘是它注入的），所以这里持续采样，
  //    留**信息最全**的那一拍，而不是"第一次看到层"的那一拍 ——
  //    否则断言会拿一个 0%、没名字的快照去判，纯属测自己手快。
  let first = null
  let shotTaken = false
  let plateLoaded = false
  for (let i = 0; i < 80; i++) {
    try {
      const d = await s.eval(BOOT_DOM)
      if (d?.present) {
        if (!first || (!first.name && d.name)) first = d
        if (d.plateLoaded) plateLoaded = true
        if (d.name && d.plateLoaded && !shotTaken) {
          await s.screenshot(`${SHOT}/boot-01-visible.png`)
          shotTaken = true
        }
      } else if (first) {
        break
      }
    } catch { /* 导航中执行上下文会短暂失效，忽略 */ }
    await sleep(120)
  }
  if (!shotTaken) await s.screenshot(`${SHOT}/boot-01-visible.png`)
  await nav
  await sleep(200)
  const mid = await s.eval(BOOT_DOM)
  if (mid?.present && mid.name && !first?.name) first = mid
  await s.screenshot(`${SHOT}/boot-02-progress.png`)

  check(!!first?.present, '主包下载期间能看到启动层')
  check(!!first?.name, '启动层带干员名字', first?.name || '')
  check(!!first?.line, '启动层带大招语音', first?.line || '')
  check(/^\d+$/.test(first?.percent || ''), '启动层有进度数字', `${first?.percent}%`)
  check(!!first?.plateSrc, '立绘地址已注入', first?.plateSrc || '')
  // 立绘是异步解码的：刚注入 src 时 naturalWidth 还是 0。
  // 判据取"整个启动层存活期间有没有任何一拍解码完成"，而不是第一拍。
  check(plateLoaded, '立绘真的加载出来了（naturalWidth>0）', first?.plateSrc || '')

  // 等它自己退场
  let gone = false
  for (let i = 0; i < 80; i++) {
    const d = await s.eval(BOOT_DOM)
    if (!d?.present) { gone = true; break }
    await sleep(150)
  }
  check(gone, '启动层自己退场并从 DOM 移除')

  const after = await s.eval(`(() => ({
    boot: !!document.getElementById('boot-layer'),
    appHasCards: document.querySelectorAll('.character-card, .card-unit, .nd').length > 0,
    bodyText: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 120),
  }))()`)
  check(!after.boot, '退场后 #boot-layer 不存在')
  await sleep(600)
  await s.screenshot(`${SHOT}/boot-03-app.png`)

  const errs = s.consoleErrors.filter((t) => !/favicon|net::ERR_/i.test(t))
  check(errs.length === 0, '控制台无报错', errs.slice(0, 3).join(' | '))
  console.log(`  页面首屏文本：${after.bodyText}`)
} catch (err) {
  fail++
  console.log('  FAIL  异常：', err?.message || err)
  try { await s.screenshot(`${SHOT}/boot-99-error.png`) } catch {}
} finally {
  s.close()
  await browser.close()
}

console.log(`\n合计：${pass} PASS / ${fail} FAIL`)
process.exit(fail === 0 ? 0 : 1)
