// =============================================================================
// 联系方式 / 相关链接(constants/contact)
// -----------------------------------------------------------------------------
// 「关于」面板(设置 → 关于)的 QQ 群入口与仓库链接,**只在这一处维护**:
// 群名 / 群号 / 分享链接 / 二维码四件东西必须一起改,分散到各处一定会走散。
//
// 3.2.2 新增的两张二维码(qr_notice = 公告群、qr_chat = 交流群):
//   源图是 QQ 群分享页导出的卡片图,已裁掉四周大片黑边、压成 webp(480×600)。
//   它们是深色底的彩色码,直接放在深色面板上就能扫 —— 但**不要**给它们加
//   任何滤镜 / 混合模式 / 透明度,否则对比度下降会扫不出来。
// =============================================================================

// 群二维码:一张公告群、一张交流群,顺序与 QQ_GROUPS 一致
import qrNotice from '../assets/materials/qr_notice.webp'
import qrChat from '../assets/materials/qr_chat.webp'

/** 一个 QQ 群入口 */
export interface QqGroup {
  /** 群名 */
  label: string
  /** 群号(点击会复制到剪贴板) */
  code: string
  /** 加群页链接(手机端会试着直接唤起 QQ,见 utils/qqGroup.ts) */
  url: string
  /** 群二维码(已裁掉原图四周的大片黑边) */
  qr: string
}

/** QQ 群入口(顺序即展示顺序) */
export const QQ_GROUPS: readonly QqGroup[] = [
  {
    label: 'BAKER AI 公告群',
    code: '1056020027',
    url: 'https://qun.qq.com/universal-share/share?ac=1&authKey=DPFveNVF1Z52nYKN7Y5hkQpiyePOyZO1tM%2B5HC3RMwbdBIfllYsZnHlF2JiSOuob&busi_data=eyJncm91cENvZGUiOiIxMDU2MDIwMDI3IiwidG9rZW4iOiJiVWRLVFpsUjZvNWN3NUdoajZJS290aWg5M1lleEJuemc2dnBWUUlsNmhSZFZYN3JySE9xRy9XUUh0dXY1cmpEIiwidWluIjoiMTc2MjQ1NTE3In0%3D&data=kxqHAM5MIP1o9MYhyyAWOUWFZtR-xuKw_s0zbIlz-qz0Jh1KUYK6kpQVwWE5AiG4S4yrUU_hloIJ8m9b4eihTw&svctype=4&tempid=h5_group_info',
    qr: qrNotice,
  },
  {
    label: 'BAKER AI 交流群',
    code: '926147159',
    url: 'https://qun.qq.com/universal-share/share?ac=1&authKey=VuPGuN9ZJgrnDk1yAYhNa1ttQMgjzWtLq3Dy4zFLeySkCrgZw9XGMAdEkyqwqLuG&busi_data=eyJncm91cENvZGUiOiI5MjYxNDcxNTkiLCJ0b2tlbiI6IjdOY1c3K1B2NnRmR2QrYTJKSklqUVZnNHRxNnBlUytLVlErYm1WdWU1bW5JQjlnQyszam1LLzFuVGlzVjMxNjIiLCJ1aW4iOiIxNzYyNDU1MTcifQ%3D%3D&data=ImB81MsUWZyM0CGFfQegtwvYV4fSFCwpojz2Bjv4-r5Ha_O8_Q8nsfO8vqvWYdVFl_WwpAFVHkfVv4PtQrmSXg&svctype=4&tempid=h5_group_info',
    qr: qrChat,
  },
]

/** 源码仓库:关于面板里单独一块,不和其它链接混在一起 */
export const GITHUB_URL = 'https://github.com/NCreeper233/endfield-baker-chat'

/**
 * 相关链接(归档的公告页「关于」页签仍在用)
 *
 * 3.2.2 的关于面板只保留 GitHub 一行(见上面的 GITHUB_URL);这两个项目入口
 * 属于 3.1 公告页的内容,页面归档后暂时不再展示 —— 常量留着,恢复公告页时
 * 不用再翻代码。
 */
export const PROJECT_LINKS: readonly { label: string; url: string }[] = [
  { label: 'GitHub', url: GITHUB_URL },
  { label: '哔哩哔哩', url: 'https://space.bilibili.com/1143315127' },
]
