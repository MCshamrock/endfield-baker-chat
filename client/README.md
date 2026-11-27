# Baker AI 4 · 客户端工程（Windows + Android）

> 版本 **4.0.5（Windows + 安卓）**｜网页层基线：`BakerAI_new/web/front` @ `bb9a738`（3.2.2 设计语言）｜
> 新客户端与旧版 **并存安装**：包名 / 安装目录 / 数据目录 / 签名全部独立，不覆盖旧版。

---

## 1. 它是什么

- **一套网页层，两个壳**：根目录就是共享的 Vue 3 + Vite 网页工程（来自最新 Baker AI Web），
  `Windows/` 是 Electron 外壳 + 终末地风格安装器，`Android/` 是 Capacitor 工程。
- **不是老客户端**：没有复用 3.0/3.1 的主进程、安装器逻辑或安卓工程；只沿用了
  3.2.2 的设计语言、原生存储接口（`window.nativeStorage`）与两个本地插件的行为约定。
- 版本号唯一来源：根 `package.json` 的 `version`（关于页、设备注册、更新比较都读它）。

### 身份卡（与旧版并存的关键）

| 项 | Baker AI 4（本工程） | 旧版（对照） |
|---|---|---|
| Windows 产品名 | `Baker AI 4` | `Baker AI` |
| Windows appId / AUMID | `com.peilika.bakerai` | `com.perlica.endfieldbaker` |
| Windows 默认安装目录 | `%LOCALAPPDATA%\Programs\Baker AI 4` | `%LOCALAPPDATA%\Programs\Baker AI` |
| 卸载登记键 | `HKCU\...\Uninstall\BakerAI4` | `...\Uninstall\BakerAI` |
| 安卓包名 | `com.peilika.bakerai` | `com.perlica.endfieldbaker` |
| 安卓 versionCode | `1`（新包名，独立版本线） | `7` |
| 安卓签名 | 新密钥 `Android/keys/bakerai4.jks`（RSA 4096） | AOSP testkey `61:ED:…` |
| 证书 SHA-256 | `BD:84:A8:FF:88:22:75:A5:7B:16:A1:A5:D4:7A:DC:27:97:E0:02:67:1E:BE:E2:E3:EF:20:56:8B:47:C8:D1:AF` | 不同 |

> 因为包名与签名都不同，安卓端**不可能**覆盖旧版；Windows 端安装目录和卸载键也都不同，
> 两个版本可以同时存在。新客户端首次启动是干净数据，旧聊天记录通过
> 「设置 → 数据管理 → 从旧版导入…」手动迁移（只读旧文件，不改不删）。

---

## 2. 目录

```
client/
├─ package.json              # 共享网页工程（4.0.5）
├─ src/ public/ index.html   # 网页层源码（设计语言沿用 3.2.2）
├─ dist/                     # npm run build 产物（Electron / APK 共用）
├─ .env.production           # 构建期注入后端：https://chat.peilika.beer
│
├─ Windows/
│  ├─ app/                   # Electron 主进程（新写：main/preload/paths/package.json）
│  └─ installer/             # C# 安装器（终末地风格：纸白 + 酸黄 + 切角）
│     ├─ src/                # Ef.cs（绘制底座）/ SetupForm / Installer / Uninstall
│     ├─ tools/              # build.py（载荷→7z→外壳→拼装）/ make_brand_assets.py
│     ├─ out/                # 中间产物与最终 Setup.exe
│     └─ payload/            # 7z 载荷（可删，构建时重建）
│
├─ Android/                  # Capacitor 工程（com.peilika.bakerai）
│  ├─ app/src/main/java/com/peilika/bakerai/  # MainActivity + SaveFile + QqGroup
│  ├─ keys/                  # 新签名密钥（不要提交/外传）
│  └─ build-apk.ps1          # 一键出包 + 对齐 + 签名 + 校验
│
└─ Releases/                 # 发版产物：<大>.x.x/<大>.<小>.x/<完整版本>/（含 SHA256SUMS 与说明）
```

---

## 3. 构建

### 3.1 网页层（两端共用）

```powershell
cd F:\dshworkspace\BakerAI\BakerAI_new\client
npm ci                 # 首次；本机已有 node_modules 可跳过
npm run build          # vue-tsc --noEmit + vite build -> dist/
```

- `VITE_BAKER_BACKEND` 已在 `.env.production` 写死为 `https://chat.peilika.beer`；
  换测试站时改这一行再 `npm run build`。
- `vite.config.ts` 的 `base: './'` 是打包端能加载相对资源的前提，别改回 `/`。

### 3.2 Windows

前置：Node 20+、Python 3.10+、.NET Framework 4 的 `csc.exe`、Electron 43.4.0 的
下载缓存（`%LOCALAPPDATA%\electron\Cache\...\electron-v43.4.0-win32-x64.zip`）。

```powershell
cd F:\dshworkspace\BakerAI\BakerAI_new\client
npm run build
python Windows\installer\tools\build.py
# 产物：Windows\installer\out\BakerAI4-v4.0.0-Setup.exe
```

或直接：

```powershell
powershell -ExecutionPolicy Bypass -File Windows\build.ps1
```

要点：

- 载荷是 **`resources/app.asar`**，不是 `resources/app` 目录。
  Electron 43 在 Windows 11 Canary（26300）上，用未打包目录启动会在 ready 阶段触发
  `0x80000003` CHECK 崩溃；打成 asar 后正常（实测）。
- 安装器默认按用户安装，不需要管理员权限；装到 `Program Files` 时才提示提权。
- 安装器支持静默参数（自动化/自测用）：
  `BakerAI4-v4.0.0-Setup.exe /S /D=<目录>`；卸载器支持 `/quiet`。
- EXE **没有代码签名证书**，首次运行可能被 SmartScreen 提示「更多信息 → 仍要运行」。

### 3.3 Android

前置：JDK 21、Android SDK（platform 36 + build-tools 36.0.0）、Gradle 8.14.3。

```powershell
cd F:\dshworkspace\BakerAI\BakerAI_new\client
npx cap copy android
cd Android
# 用本机工具链时（示例）：
$env:JAVA_HOME='D:\tmp\baker4-build\jdk\jdk-21.0.2'
$env:ANDROID_HOME='D:\tmp\baker4-build\sdk'
D:\tmp\baker4-build\gradle\gradle-8.14.3\bin\gradle.bat assembleRelease --no-daemon
# 再用 build-apk.ps1 里的 zipalign + apksigner 步骤签名
```

或一键：

```powershell
powershell -ExecutionPolicy Bypass -File Android\build-apk.ps1
```

产物：`Releases\BakerAI4-v4.0.0.apk`（已 zipalign + v2/v3 签名）。

> 换签名密钥会改变应用身份：新密钥只能装新包名。`Android/keys/keystore.properties`
> 里是口令，**不要提交到公开仓库**；丢了就再也签不出可覆盖升级的同包名包。

---

## 4. 数据与「从旧版导入」

### Windows

```
<安装根>\
  app\Baker AI 4.exe
  app\resources\app.asar
  bakerai_data\endfield-baker-data.json   ← 聊天记录
  bakerai_webdata\                        ← localStorage / IndexedDB
  bakerai_logs\ startup.log / error.log
  bakerai_backups\                        ← 每次启动备份，留 10 份
  bakerai_exports\                        ← 「另存为」默认目录
```

安装根不可写时回退 `%APPDATA%\BakerAI4`。

### Android

`@capacitor/filesystem` 把同一份 `endfield-baker-data.json` 写在应用数据目录；
卸载应用会一起删除，重要记录请先导出。

### 从旧版导入

`设置 → 数据管理 → 从旧版导入…` → 选旧客户端的
`<旧安装目录>\bakerai_data\endfield-baker-data.json`（或旧版导出的 JSON）。

- 只读旧文件；不移动、不删除、不改写。
- 兼容两种旧格式：整库 `cards` 数组；3.2.2 的 `cardsV2` + `card:<slot>` 分片。
- 走与旧数据迁移相同的上传管线：登录时挂账号，未登录先挂当前设备。
- 手动导入不认「已迁移」标记，可以把另一台设备的存档也并进来；重复导入会产生重复会话。

---

## 5. 已验证（2026-11-21）

| 项 | 结果 |
|---|---|
| 网页层 `vue-tsc --noEmit` | 0 错 |
| `vite build` | 成功（主包约 1.31 MB / gzip 503 KB） |
| Windows 客户端冒烟（asar） | 页面加载、`window.nativeStorage` 存在、`/v1/meta` = 200 |
| 设置 → 数据管理 | 「从旧版导入…」按钮存在；关于页版本 4.0.0 |
| 旧存档解析单测 | 整库 1 卡/1 段/2 条；分片 2 卡/2 段/2 条 |
| 静默安装 | exit 0；`app.asar` 落地；卸载登记 `Baker AI 4` / `4.0.0` |
| 静默卸载 | exit 0；程序与登记删除；`bakerai_*` 数据保留 |
| APK 包信息 | `com.peilika.bakerai` / versionCode 1 / 4.0.0 / label `Baker AI 4` |
| APK 签名 | v2 + v3 通过；新证书（见上表） |
| APK 原生插件 | `SaveFilePlugin`、`QqGroupPlugin` 均在 dex |
| 与旧版隔离 | 旧包 `com.perlica.endfieldbaker`；新包名不同，不会覆盖 |

4.0.1（两端同版）追加验证：

| 项 | 结果 |
|---|---|
| 安全区脚本 | APK 内 `index.html` 含 `BakerInsets` / `__bakerSafeTop` |
| 旧内核兜底 | 构建产物每个 `inset` 前都有 `top/right/bottom/left` 长写 |
| 模拟老内核 | 删掉产物里全部 `inset` 声明后，设置面板仍是全屏 + 遮罩正常 |
| 模拟刘海 44px | 顶栏页签下移 44px；聊天页整体下移且底部输入框不越界；关于页显示「内核 Chrome/xx」 |
| APK 包信息 | versionCode 2 / versionName 4.0.1 / 签名证书与 4.0.0 一致 |
| 真机（V2502DA / Android 17） | 状态栏让位、设置面板全宽、数据页、聊天页、关于页逐屏看过 |
| Windows 4.0.1 | 冒烟通过（桥 / 后端 200 / 导入入口）；静默装到自定义目录，注册表 `4.0.1`；静默卸载干净 |
| 群聊 @ 图标 | 站长手绘 PNG 已进包，45px 圆形按钮铺满（预览图核对过） |

4.0.2（只发安卓）追加验证：

| 项 | 结果 |
|---|---|
| 修的问题 | 三键式导航把底部输入面板切掉一半（输入框 / 发送键只剩上半截） |
| 模拟三键导航 | 无头 Edge 412×915 注入 `__bakerSafeBottom=48`：`.m-chat` 高度 915→867，输入面板底边 914→866，正好让出 48px |
| 键盘不重复让位 | `chatViewportHeight` 在键盘弹出时只减顶部安全区（否则面板与键盘之间留缝） |
| 贴底浮条 | 删除模式 / 多选导出 / 群聊「继续对话」的 `bottom` 都加了 `var(--safe-bottom)` |
| APK 包信息 | versionCode 3 / versionName 4.0.2 / 签名证书与 4.0.0、4.0.1 一致 |
| 包内自检 | `assets/public/index.html` 有 `__bakerSafeBottom`；`index-*.js` 有对应读取逻辑；CSS 9 处 `--safe-bottom` |
| 真机 | **这一版没做**（打包时没连手机），需要三键导航机型上复看一眼聊天页底部 |

4.0.3（只发安卓）追加验证：

| 项 | 结果 |
|---|---|
| 修的问题 | 9000 条消息的会话打开要 8 秒、进去后回复也卡（用户实测 7.8 秒） |
| 打开耗时（无头 Edge 412×915，同一份 9000 条存档） | 8,760 ms → **444 ms**（最后一条消息可见 9,911 → 475 ms） |
| DOM / 内存 | 189,061 个节点 → **711**；JS 堆 327 MB → **48 MB** |
| 流式帧间隔 | p95 385 ms → **18 ms**；最长卡顿 828 → 206 ms；长任务 13 个 → 2 个 |
| 滚动回归 | 滚到顶 / 中间 / 底都正常出图，行间无重叠（`_tmp/perf-scroll-*.png`） |
| 做法 | 批量测量（整批一次布局）+ 窗口化渲染（只挂视口附近几十行）+ 按消息增量布局 + 写库存空闲时段 |
| APK 包信息 | versionCode 4 / versionName 4.0.3 / 签名证书与 4.0.0–4.0.2 一致 |
| 包内自检 | `index-*.js` 有批量测量的隐藏容器样式、`requestIdleCallback`、版本串 `4.0.3` |
| 真机 | **这一版没做**（打包时没连手机）；低端机量级会放大，方向一致 |
| 发送失败（感叹号） | 客户端未复现；生产后端日志里查到 MySQL 锁等待超时（1205）与流式断连，属服务端问题，另记 |

4.0.4（Windows + 安卓）追加验证：

| 项 | 结果 |
|---|---|
| 修的问题 | 25MB / 1.2 万条消息的存档点「导出数据」后按钮一直灰着、无任何反馈（用户录屏 15 秒两个按钮都停在「导出中…」） |
| 真因 | ① 导出链没有进度反馈（`onProgress` 从未接线）；② 整包 blob→35MB base64→再解码回字节的往返；③ JSON 把 30MB 字符串 + 41MB base64 拼在内存里；④ 打包段无超时 |
| 打包段（真导出代码，12512 条 / 24.8M 字符） | ZIP 1361 → **1154 ms**，峰值 rss 354 → **178 MB**；JSON 1426 → **1197 ms**，414 → **197 MB** |
| 界面（无头 Edge 412×915，7500 条 / 14.9MB） | 点导出后能看到「正在打包数据… 0s」→ 完成提示；导出数据 564 ms、导出 JSON 271 ms，结束后按钮恢复 |
| 单测 | Node + 假 Capacitor 插件 **19 项全过**（含逐块 SHA-256、Electron 大文件、JSON 流式与同步版一致） |
| 顺带修的 | `getPlugin()` 改成同步（`await` Capacitor 代理会调 `.then()` 卡住/抛错）；`sha256Hex` 分片改为按 offset/length 显式切；Windows 大存档（>1.5MB）导出报"不支持分块写入" |
| APK 包信息 | versionCode 5 / versionName 4.0.4 / 签名证书与 4.0.0–4.0.3 一致 |
| 包内自检 | `index-*.js` 有「正在打包数据」「正在写入」「正在校验文件」与版本串 `4.0.4` |
| 真机 | 安卓**这一版没做**（打包时没连手机）；4.0.4 会把过程显示出来，下次能直接看出卡在哪一步 |
| Windows 安装器 | 主界面「本次更新」卡片改由 `Notes.cs` 驱动（覆盖 4.0.1 → 4.0.4 四条），载荷《使用说明》开头同步补一段 |
| Windows 验收 | `tools/verify.py` **28 项全过**（静默装 5.4s → 文件/asar/卸载登记 4.0.4 → 起应用 CDP 读到页面与桥 → 静默卸载无残留） |
| Windows 包信息 | 文件/产品版本 `4.0.4.0`；Setup 100,937,623 B，`ffc5d5a7…` |

4.0.5（Windows + 安卓）追加验证：

| 项 | 结果 |
|---|---|
| 修的问题 | 4.0.4 在一段话分成多行时只显示第一段，后面几行被裁（用户两张截图） |
| 真因① | `canvasEstimate` 数行写错：`lines` 从 1 起、每段只补 `ceil(w/innerMax)-1` → 三段各一行只算出 1 行 |
| 真因② | 批量实测回来后只清了行缓存，`msgCache` 仍复用"用估算排的行" → 精确高度永远落不了地 |
| 改法 | 估算按段数行；`flush()` 回传 `{count, texts}`；`useChatRows` 只作废被实测过的那几条（`MsgLayout.probe`），后缀重排、前缀照旧复用 |
| 多行气泡实测（无头 Edge 412×915，真 dist） | 三段回复 94px / 两段长文本 194px，全部与文本真实高度一致；**9 个气泡 0 个被裁**（修复前三段回复按 34px 分配） |
| 记忆关闭 | `constants/features.ts` 的 `MEMORY_ENABLED = false`：设置页签实测为 账号/API/提示词/显示/实验/数据/关于（无「干员记忆」），新建会话 `memory:'off'`，本地偏好不接受写入；面板/接口/后端/已记数据全部保留 |
| Windows 安装器 | 「本次更新」三条（多行不截断 / 按真实行数 / 记忆暂时关闭）；`verify.py` **28 项全过**（卸载登记 Baker AI 4 / 4.0.5） |
| APK 包信息 | versionCode 6 / versionName 4.0.5 / 签名证书与 4.0.0–4.0.4 一致 |
| 真机 | **这一版没做**（打包时没连手机） |

截图：`Releases/screenshots/`。

---

## 6. 发版清单

1. 改根 `package.json` 的 `version`（例如 4.0.1）。
2. Windows：`Windows/installer/tools/build.py` 里 `VERSION` 同步；重新构建。
3. Android：`Android/app/build.gradle` 的 `versionCode` **必须递增**、`versionName` 同步。
4. `npm run build` → `npx cap copy android` → 重新签名。
5. 成品落到 `Releases/<大>.x.x/<大>.<小>.x/<完整版本>/`，重算该目录的 `SHA256SUMS.txt`。
6. 更新 `Releases/README.md` 的版本表与本文件的「已验证」表。

---

## 7. 兼容性（安卓老 WebView）

APK 用的是**系统 WebView**，不是打包进去的浏览器内核：用户不更新，它就一直停在老版本
（线上确实有 Chrome 8x 的机器）。两条底线：

- **JS 语法**：`build.target = es2019`，构建期降级；内置方法在 `src/main.ts` 里补
  （`String.prototype.replaceAll`、`Array.prototype.at`）。
- **CSS**：`inset` 简写是 Chrome 87 才有的，老内核会整条丢掉 —— 表现就是弹窗缩成内容宽、
  遮罩消失。构建期由 `vite.config.ts` 的 `insetFallback` 插件展开成四条长写
  （PostCSS 钩子 + `generateBundle` 钩子；压缩器会把长写又合并回简写，所以必须在压缩后再补一次）。
  **源码继续写 `inset` 就行，别手写四遍。**

同理，`aspect-ratio`（Chrome 88）在关键位置要配一个显式高度兜底；`gap`（flex，Chrome 84）
低于这个版本的机器已不在支持范围内。

## 8. 安全区（状态栏 / 挖孔 / 手势条）

- 原生侧：`MainActivity` 用 `WindowInsets`（`systemBars | displayCutout`）量出**网页与系统栏的真实重叠量**，
  通过 `BakerInsets` 接口（同步读）+ `evaluateJavascript`（变化时推送）交给网页。
  老系统（窗口本来就让开系统栏）重叠量为 0，不会多补。
- 网页侧：`index.html` 的内联脚本在首屏前写入 `--safe-top/--safe-right/--safe-bottom/--safe-left`
  与 `window.__bakerSafeTop`；随后 `AppHeader`（含品牌标题的垂直居中）、设置面板（`.pn-win` / `.pn-body`）、
  移动端聊天（`useMobile` 的 `chatViewportHeight` + `.m-chat` 的 top）各自消费。
- 为什么不用 `env(safe-area-inset-*)`：安卓 WebView 只反映刘海，拿不到状态栏高度。

## 9. 已知事项

- 生产公告 `notice.peilika.beer/popup.json` 里那条「特别鸣谢」提醒已在 2026-11-21 从服务端删除
  （备份 `popup.json.bak_thanks_20261121_153513`）；公告正文仍是「只针对于网页端，客户端请无视」的
  紧急通知，新客户端会照常弹出（服务端驱动）。建议在后台改成面向所有人的版本。
- 关于页会显示「内核 Chrome/xx」：安卓老内核导致的错位，先看这一行再判断要不要补兼容。
- Windows EXE 未签名（无证书），SmartScreen 提示属预期。
- 安卓工具链（JDK/SDK/Gradle）没有放进仓库；`D:\tmp\baker4-build` 是本机缓存，
  换机器需按 3.3 重新准备。
- 安装器/客户端的中文路径、空格路径都已实测；不要在安装目录里手动改 `app.asar`。
