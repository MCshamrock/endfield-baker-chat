import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 面板的构建配置。两条硬约束决定了这里的写法：
 *
 * 1. **开发时不改地址**：页面里所有请求都是相对路径 `/v1/...`，dev 由这个代理
 *    转到本机 5660，生产由 nginx 同源反代。同一份代码两处都能跑，不会出现
 *    "本地好、线上跨域"。
 * 2. **产物直接丢给 nginx 当静态站**：base 用 '/'（站点挂在域名根），
 *    SPA 的深链接靠 nginx 的 try_files 兜，所以这里不用配 router 的 base。
 */
export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    // 后台只在现代 Edge/Chrome 里看，但 es2019 是 web/front 的同款口径，
    // 万一有人用旧安卓平板也能开（语法降级由构建期完成，运行时零成本）。
    target: 'es2019',
    sourcemap: false,
    // 面板就一个包，体积很小；阈值压到 300KB 让误引入大依赖立刻显形
    chunkSizeWarningLimit: 300,
  },
  server: {
    // 后台的 dev server 固定 5273：5660 是后端、5173 是 web/front，错开一眼能认。
    port: 5273,
    strictPort: true,
    proxy: {
      // 后端地址可用环境变量覆盖，方便临时连别的机器
      '/v1': {
        target: process.env.BAKER_DEV_BACKEND || 'http://127.0.0.1:5660',
        changeOrigin: true,
      },
    },
    watch: {
      // Windows 上编辑器原子保存会产生临时目录，chokidar 撞上会 EBUSY 崩掉
      ignored: ['**/.*.tmpdir/**', '**/.*.tmp', '**/node_modules/**', '**/.git/**'],
    },
  },
  preview: {
    // 预览的是"真正要部署的那份 dist"。代理和 dev 一致，这样上线前可以用
    // 构建产物 + 真后端走一遍（比只看 dev server 更接近生产）。
    port: 5274,
    strictPort: true,
    proxy: {
      '/v1': {
        target: process.env.BAKER_DEV_BACKEND || 'http://127.0.0.1:5660',
        changeOrigin: true,
      },
    },
  },
})
