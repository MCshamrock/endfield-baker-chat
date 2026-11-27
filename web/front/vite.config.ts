import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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
