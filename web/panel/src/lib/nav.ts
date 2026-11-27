/**
 * nav.ts —— 侧栏/命令栏的唯一清单。
 *
 * 页面顺序就是信息架构：先看规模（总览），再管人（用户/干员），
 * 然后是"改了就立刻影响线上"的两块（配置/密钥），最后是查账（用量/思考链）。
 * 新增页面只改这里 + App.vue 的一张映射表，不会出现"导航里有、路由里没有"。
 */

export type SectionKey =
  | 'overview'
  | 'users'
  | 'characters'
  | 'config'
  | 'keys'
  | 'usage'
  | 'reasoning'

export interface Section {
  key: SectionKey
  /** 中文名（粗体那行） */
  zh: string
  /** 英文眉标（宽体大写，`// ` 前缀在组件里加） */
  en: string
  /** 一句话说明这一页能干什么，空状态和页头都用它 */
  hint: string
}

export const SECTIONS: Section[] = [
  { key: 'overview', zh: '总览', en: 'Overview', hint: '全站规模、密钥池、今日用量、近 7 天请求量' },
  { key: 'users', zh: '用户', en: 'Accounts', hint: '搜账号、改角色与状态、改显示名' },
  { key: 'characters', zh: '干员', en: 'Operators', hint: '上下架、发言权重、排序与提示词版本' },
  { key: 'config', zh: '配置', en: 'Config', hint: '安全协议、世界观这类热改文本，保存即刻生效' },
  { key: 'keys', zh: '密钥', en: 'Key Pool', hint: '只读状态：启停、冷却、失败次数、上次使用' },
  { key: 'usage', zh: '用量', en: 'Usage', hint: '按天汇总 7 / 30 / 90 天' },
  { key: 'reasoning', zh: '思考链', en: 'Reasoning', hint: '按会话 public_id 查最近的思考过程' },
]

export function sectionOf(key: string): Section {
  return SECTIONS.find((s) => s.key === key) ?? SECTIONS[0]
}

/** hash 路由：`#/users` → `users`。空 hash 落总览，认不出的也落总览。 */
export function readRoute(): SectionKey {
  const raw = location.hash.replace(/^#\/?/, '').split('?')[0]
  const hit = SECTIONS.find((s) => s.key === raw)
  return hit ? hit.key : 'overview'
}

export function go(key: SectionKey): void {
  location.hash = `#/${key}`
}
