# BakerAI 后台面板（panel.peilika.beer）

Vue 3 + Vite + TypeScript，**不引 UI 组件库**，样式自己写，视觉遵守 `endfield-ui`
那套规则（纸墨 / 单一酸性黄 / 切角 = 可点 / 编号行 / 细线分区 / 亮暗两套）。

```
src/
├── App.vue                  壳：登录闸门、命令栏、hash 路由
├── main.ts                  入口（样式顺序：套件 → 自己的布局）
├── assets/
│   ├── endfield/            设计套件（endfield.css + 等高线 + fx.js），原样复制
│   └── fonts/Saira.ttf      宽体拉丁字体（SIL OFL，许可证同目录）
├── components/
│   ├── LoginView.vue        登录页
│   ├── BarChart.vue         直方图（原生 CSS 柱，不引图表库）
│   └── StateBlock.vue       加载 / 空 / 失败 三态
├── views/                   8 个页面：overview / users / characters / config / keys / usage / reasoning
├── lib/
│   ├── api.ts               唯一的网络出口（token、401、403、错误体）
│   ├── format.ts            数字/时间格式化（全部集中在这里）
│   ├── session.ts           主题与"上次是谁"
│   ├── nav.ts               分区清单 + hash 路由
│   └── types.ts             后端接口形状（照 api/admin.py 抄）
└── styles/panel.css         布局与后台特有零件（不写颜色值，全用 --ef-* 变量）
```

## 命令

```bash
npm ci && npm run build     # 构建（vue-tsc --noEmit && vite build）
npm run dev                 # http://localhost:5273，/v1 代理到 127.0.0.1:5660
npm run smoke               # 假后端 23 条接口层断言
npm run check:live          # 真后端 13 条契约断言（需要 BAKER_PANEL_USER / PASS）
npm run check:ui            # 无头 Edge 走查 + 截图（需要 dev server 在跑）
```

部署步骤、复验命令、回滚、故障排查：见 [`docs/00-部署.md`](docs/00-部署.md)。
nginx 站点配置：[`nginx/panel.peilika.beer.conf`](nginx/panel.peilika.beer.conf)。

## 三条不许破的规矩

1. **不显示任何密钥明文**。后端 `/v1/admin/keys` 连密文都不返回；页面里也没有可显示的字段。
2. **不把 token 打到 console**。页面里没有一处 `console.*`。
3. **数字都是真的**。没有的统计（比如密钥成功率/延迟）就不显示，不拿别的数凑。
