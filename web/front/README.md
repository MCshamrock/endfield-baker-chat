# BakerAI 网页前端（web/front）

> 新后端 [`web/back`](../back) 的网页前端。
> 基座 = **服务器上最新的网页源码**（`baker-test/frontend/endfield-baker-chat-v2`，
> 2026-11-14 版，经 frp 拉取）的 web 部分。
> 中间层按新后端 `/v1` 契约重写（单聊 + 群聊 + 附件 + 推荐 + 账号），
> 并补了账号页与群聊「加权模式 / 发言权重」入口。
>
> **工程文档在 [`docs/`](docs/)** —— 先看 [`docs/00-工程总览.md`](docs/00-工程总览.md)。

## 快速开始

```powershell
# 后端：127.0.0.1:5660（方法见 docs/04-验收与自测.md）
cd ..\back
.venv\Scripts\uvicorn.exe app.main:app --port 5660

# 前端
cd ..\front
npm ci
npm run dev      # http://localhost:5173（/v1、/group 已代理到 5660）
```

生产构建：`npm run build` → `dist/`（同源部署，反代 `/v1`、`/group` 到后端）。

## 自测

```powershell
node scripts/contract-smoke.mjs   # 27 项，端点级
node scripts/adapter-e2e.mjs      # 16 项，单聊中间层
node scripts/group-e2e.mjs        # 24 项，群聊中间层
```

## 文档

| 文件 | 内容 |
|---|---|
| [00-工程总览](docs/00-工程总览.md) | 是什么/不是什么、目录、跑法、来源 |
| [01-中间层适配](docs/01-中间层适配.md) | 旧模型 → 新契约、会话绑定、导入、幂等、流式、群聊 |
| [02-登录与账号](docs/02-登录与账号.md) | 设备 token、登录不换 token、错误码、数据归属 |
| [03-缺陷修复记录](docs/03-缺陷修复记录.md) | 每条 bug 的现象/根因/修法/验证 |
| [04-验收与自测](docs/04-验收与自测.md) | 起服务、自动自测、界面 10 步、13 条清单对照 |
| [05-已知差异与待办](docs/05-已知差异与待办.md) | 有意的边界与下一步 |
| [06-旧版本地数据迁移](docs/06-旧版本地数据迁移.md) | 老 IndexedDB/localStorage 清单、web31 与 3.2.2 差异、迁移实现与覆盖部署 checklist |
| [07-服务端驱动列表](docs/07-服务端驱动列表.md) | 列表/会话/消息改由 `/v1` 当真相：现状盘点、目标数据流、三阶段与验收、后端需求、与 fe-migrate 的边界 |
| [08-总结与压缩归属](docs/08-总结与压缩归属.md) | 压缩/总结由服务端负责（新 web 不做客户端总结）、前端死路径清单、界面文案口径 |

## 素材来源

| 素材     | 来源                                                         | 备注                   |
| -------- | ------------------------------------------------------------ | ---------------------- |
| 角色头像 | [森空岛《明日方舟：终末地》WIKI](https://wiki.skland.com/endfield) | 素材版权为鹰角网络所有 |
| 干员资料 | [森空岛《明日方舟：终末地》WIKI](https://wiki.skland.com/endfield) | 素材版权为鹰角网络所有 |
| 表情     | 《明日方舟：终末地》游戏内                                   | 素材版权为鹰角网络所有 |
| 其他资源 | 《明日方舟：终末地》游戏内                                   | 素材版权为鹰角网络所有 |
| 字体     | [HarmonyOS Sans](https://developer.huawei.com/consumer/cn/design/resource-V1/) |                        |

## 参考项目

- https://github.com/Wanye-7300/baker-dx
- https://github.com/blacktunes/sr-message-maker

## 相关项目

- 明日方舟：终末地风格LOGO生成器
  - https://ark.ncreeper.top/
  - https://github.com/NCreeper233/endfield-logo-maker/
- 明日方舟：终末地 Baker 模拟器
  - https://baker.ncreeper.top/
  - https://github.com/NCreeper233/endfield-baker-maker/
