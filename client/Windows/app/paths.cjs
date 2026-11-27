// paths.cjs —— Baker AI 4 的数据根目录判定（主进程与 preload 共用）
//
// 安装布局（与旧版 Baker AI 完全分开，互不覆盖）：
//   <安装根>\app\Baker AI 4.exe      程序本体
//   <安装根>\bakerai_data            存档 endfield-baker-data.json（+ .bak）
//   <安装根>\bakerai_webdata         Chromium 的 localStorage / 缓存
//   <安装根>\bakerai_logs            启动 / 错误日志
//   <安装根>\bakerai_backups         每次启动的存档备份（留最近 10 份）
//   <安装根>\bakerai_exports         「另存为」默认目录
//
// 数据放在安装根而不是 app\ 里：卸载时删掉整个 app\ 也碰不到聊天记录。
// 安装根不可写（Program Files、只读介质）→ 回退 %APPDATA%\BakerAI4，不阻断启动。

const path = require('path')
const fs = require('fs')

/** 主进程启动时保证存在的五个目录 */
const DATA_DIRS = [
  'bakerai_data',
  'bakerai_logs',
  'bakerai_webdata',
  'bakerai_backups',
  'bakerai_exports',
]

/** 写探针：真的能写才算可写，不用 access 之类的间接判断 */
function isWritable(dir) {
  try {
    const probe = path.join(dir, '.write-probe-' + process.pid)
    fs.writeFileSync(probe, 'ok')
    fs.unlinkSync(probe)
    return true
  } catch {
    return false
  }
}

/**
 * @returns {{ exeDir: string, programDir: string, dataRoot: string, fallback: boolean }}
 *   exeDir     exe 所在目录（<安装根>\app）
 *   programDir 安装根（exe 目录名不是 app 时就是 exe 目录）
 *   dataRoot   五个 bakerai_* 目录的父目录
 *   fallback   是否回退到了 %APPDATA%
 */
function resolveRoots() {
  // path.resolve 先规范化：用 `./Baker AI 4.exe` 启动时 execPath 可能带 `\.\`，
  // 直接 basename 会得到 "."，数据就会落进 app\ 里而不是安装根。
  const exeDir = path.resolve(path.dirname(process.execPath))
  const programDir = path.basename(exeDir).toLowerCase() === 'app' ? path.dirname(exeDir) : exeDir

  let dataRoot = programDir
  let fallback = false
  if (!isWritable(dataRoot)) {
    // 新品牌的新回退目录：不和老版的 PerlicaAI 混用
    dataRoot = path.join(process.env.APPDATA || process.env.HOME || programDir, 'BakerAI4')
    fallback = true
  }
  return { exeDir, programDir, dataRoot, fallback }
}

module.exports = { DATA_DIRS, isWritable, resolveRoots }
