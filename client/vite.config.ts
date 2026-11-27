import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import type { Plugin } from 'postcss'

/**
 * 旧内核兜底：把 `inset` 简写拆成 top/right/bottom/left 四条长写。
 *
 * 为什么需要：`inset` 是 Chrome 87 才有的简写，而安卓 APK 用的是**系统 WebView**
 * （用户不更新就一直是老版本）。老内核整条声明直接忽略，`position: fixed` 的
 * 弹窗就没有偏移量 —— 结果是面板缩成内容宽、贴在页面流里，遮罩也不见了
 * （Redmi K30 Pro 上的 4.0.0 就是这样）。拆成四条后新老内核都认。
 *
 * 源码继续写 `inset`（可读），构建期自动补，别再手写四遍。
 */
function insetFallback(): Plugin {
  const SIDES = ['top', 'right', 'bottom', 'left'] as const

  /** 按 CSS 的 1~4 值规则展开；解析不了（var()/多值异常）就原样返回 null */
  function expand(value: string): Record<string, string> | null {
    const parts = value.trim().split(/\s+/).filter(Boolean)
    if (parts.length < 1 || parts.length > 4) return null
    const [a, b = a, c = a, d = b] = parts
    return { top: a, right: b, bottom: c, left: d }
  }

  return {
    postcssPlugin: 'baker-inset-fallback',
    Declaration(decl) {
      if (decl.prop !== 'inset') return
      // @supports 里的是特性探测，别动
      if (decl.parent?.type === 'atrule' && (decl.parent as { name?: string }).name === 'supports') return
      const sides = expand(decl.value)
      if (!sides) return
      for (const side of SIDES) decl.cloneBefore({ prop: side, value: sides[side] })
    },
    /**
     * 打包完还要再补一次。
     *
     * esbuild 的 CSS 压缩器会把 top/right/bottom/left: 0 又合并回 inset:0
     * （它按最新内核输出），所以只在 Declaration 里补是白费劲 —— 必须在
     * compress 之后的产物上再展开一遍。
     */
    generateBundle(_options: unknown, bundle: Record<string, any>) {
      for (const file of Object.values(bundle)) {
        if (!file || file.type !== 'asset' || !String(file.fileName).endsWith('.css')) continue
        const css = typeof file.source === 'string' ? file.source : Buffer.from(file.source).toString('utf8')
        file.source = css.replace(/(?<![\w-])inset:([^;}]+)/g, (whole: string, raw: string) => {
          const value = raw.trim()
          // !important 单独摘出来，展开后每条都要带
          const bang = /\s*!important\s*$/.test(value) ? '!important' : ''
          const sides = expand(value.replace(/\s*!important\s*$/, ''))
          if (!sides) return whole
          return SIDES.map((s) => `${s}:${sides[s]}${bang ? ' !important' : ''}`).join(';') + ';' + whole
        })
      }
    },
  }
}

export default defineConfig({
  // 同一个插件挂两处：
  //   · plugins 里的是 rollup 钩子（generateBundle，压缩完再补一次）
  //   · css.postcss.plugins 里的是 PostCSS 钩子（开发服务器 / 未压缩产物）
  plugins: [vue(), insetFallback()],
  css: {
    postcss: {
      // 只挂自己的补丁；没有 postcss.config，不会盖掉别的插件
      plugins: [insetFallback()],
    },
  },
  base: './',
  build: {
    // 语法目标取三端 WebView/Electron 的最低公共集:esnext 产物在旧安卓
    // WebView 上会直接语法报错(白屏),es2019 由构建期降级、运行时零成本。
    target: 'es2019',
    // 体积告警阈值:主包本就接近 1MB,默认 500KB 的告警形同虚设;
    // 收到 350KB 后任何新增大依赖都会在构建日志里立刻显形。
    chunkSizeWarningLimit: 350,
    // 生产包不带 sourcemap:三端都以包体积/安装包大小为先,调试走 dev 构建。
    sourcemap: false,
  },
  // Worker 用 ESM 输出：ZIP 打包 Worker 会间接引用导出层（含 Capacitor 插件），
  // 而 iife（Vite 旧默认）不支持代码分割构建 —— 2026-09-29 构建报
  // "Invalid value iife for option worker.format" 就是这么来的。
  worker: {
    format: 'es',
  },
  server: {
    // 开发代理：前端跑在 5173，后端（web/back）跑在 5660。
    // 页面里用相对路径（同源）即可，不必在开发机上改地址，也不会撞 CORS。
    // 后端地址可用环境变量覆盖：BAKER_DEV_BACKEND=http://192.168.1.5:5660
    proxy: {
      '/v1': {
        target: process.env.BAKER_DEV_BACKEND || 'http://127.0.0.1:5660',
        changeOrigin: true,
      },
      '/group': {
        target: process.env.BAKER_DEV_BACKEND || 'http://127.0.0.1:5660',
        changeOrigin: true,
      },
      '/readyz': {
        target: process.env.BAKER_DEV_BACKEND || 'http://127.0.0.1:5660',
        changeOrigin: true,
      },
    },
    watch: {
      // 忽略编辑器/工具原子保存产生的临时目录与临时文件
      // (如 .App.vue.2424.xxx.tmpdir/App.vue.tmp),避免 chokidar
      // 在 Windows 上 watch 到被锁/瞬时的文件触发 EBUSY 崩溃
      ignored: [
        '**/.*.tmpdir/**',
        '**/.*.tmp',
        '**/*.tmp',
        '**/node_modules/**',
        '**/.git/**',
      ],
    },
  },
})
