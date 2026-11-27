// =============================================================================
// 群聊 @ 提及（mention）
// -----------------------------------------------------------------------------
// 纯函数 + 一张名字→slug 的表，UI（ChatInput 的浮层）与发送链路
// （useGroupChat → groupBackend）共用，所以单独一个文件、方便单测。
//
// 后端口径（web/back `app/schemas.py:223`、`app/group.py:184`）：
//   - `POST /v1/conversations/{id}/messages` 与 `.../next` 接受 `mentions: [slug]`，
//     **最多 8 个**；
//   - 后端还会从正文里兜底解析「@明河」，但我们要**显式传 slug**，不依赖文本解析。
//
// 为什么不做「@全体成员」：后端没有 @all 语义，做了等于骗人（产品决定，见任务书）。
// =============================================================================

/** 一条提及选项（浮层里显示：头像 + 名字） */
export interface MentionCandidate {
  /** 角色 slug（/v1/characters 的 id；找不到时退回名字） */
  slug: string
  /** 角色名（插入文本用的就是它） */
  name: string
}

/** 输入框里"正在输入的 @ 片段" */
export interface MentionTrigger {
  /** `@` 在文本里的下标 */
  start: number
  /** `@` 之后已经输入的内容（用于过滤，不含 @） */
  query: string
}

/** 后端允许的提及上限（与 schemas.py 一致） */
export const MAX_MENTIONS = 8

/** `@` 片段最长允许多少个字符（超过就不当提及了，避免整段话都在过滤） */
const MAX_QUERY_LEN = 12

/**
 * 从"光标前的文本"里找出正在输入的 @ 片段
 *
 * 判据：最后一个 `@` 之后没有空格、换行或第二个 `@`，且长度不超过 MAX_QUERY_LEN。
 * 例：
 *   "@"        → { start:0, query:'' }     （刚敲下 @，列出全部成员）
 *   "你好 @明"  → { start:3, query:'明' }
 *   "@明河 "    → null                     （已经用空格结束了）
 *
 * @param beforeCaret 光标之前的纯文本
 */
export function findMentionTrigger(beforeCaret: string): MentionTrigger | null {
  const text = beforeCaret ?? ''
  const at = text.lastIndexOf('@')
  if (at < 0) return null
  const tail = text.slice(at + 1)
  if (tail.length > MAX_QUERY_LEN) return null
  // 允许汉字/字母/数字，出现空白或第二个 @ 就说明这段已经结束了
  if (/[\s@]/.test(tail)) return null
  return { start: at, query: tail }
}

/**
 * 按已输入的内容过滤成员
 *
 * 规则：名字包含关键词即命中（大小写不敏感）；空关键词 = 列出全部。
 * 顺序保持传入顺序（群成员顺序），不重排 —— 玩家对"谁在第几个"有肌肉记忆。
 */
export function filterMentionCandidates(
  candidates: MentionCandidate[],
  query: string,
): MentionCandidate[] {
  const kw = (query ?? '').trim().toLowerCase()
  if (!kw) return candidates.slice()
  return candidates.filter((c) => c.name.toLowerCase().includes(kw))
}

/**
 * 把选中的名字插进输入框文本
 *
 * **不是替换整条**（任务书要求）：只把光标前那段 `@query` 换成 `@名字 `。
 *
 * @param text   当前整段文本
 * @param caret  光标位置（text 的下标）
 * @param name   选中的角色名
 * @returns 新文本与新光标位置
 */
export function insertMention(
  text: string,
  caret: number,
  name: string,
): { text: string; caret: number } {
  const value = text ?? ''
  const pos = Math.max(0, Math.min(value.length, caret))
  const before = value.slice(0, pos)
  const trigger = findMentionTrigger(before)
  const start = trigger ? trigger.start : pos
  const inserted = `@${name} `
  return {
    text: value.slice(0, start) + inserted + value.slice(pos),
    caret: start + inserted.length,
  }
}

/**
 * 从已经发出的文本里找出"确实提到了谁"
 *
 * 只认 `@名字`（名字必须完整匹配成员名，避免"@明"命中"明河"造成误伤）。
 * 名字可能互为前缀（"明" / "明河"），所以**按名字长度倒序**匹配。
 *
 * @returns slug 列表（去重、最多 MAX_MENTIONS 个）
 */
export function extractMentionSlugs(
  text: string,
  members: string[],
  nameToSlug: Record<string, string> = {},
): string[] {
  const value = text ?? ''
  if (!value.includes('@')) return []
  // 长的排前面：同一个位置优先匹配更长的名字（"明河" 优先于 "明"）。
  // 2026-11-18 修（审计 D4）：原来用 includes，`@明河` 会同时命中成员「明」，
  // 把没被点名的人也塞进 mentions。现在只在 **`@` 所在位置**做前缀匹配。
  const names = [...members].filter(Boolean).sort((a, b) => b.length - a.length)
  const slugs: string[] = []
  for (let i = 0; i < value.length; i++) {
    if (value[i] !== '@') continue
    for (const name of names) {
      if (!value.startsWith(name, i + 1)) continue
      const slug = nameToSlug[name] || name
      if (!slugs.includes(slug)) slugs.push(slug)
      // 跳过整个名字：这样 "@明河" 不会在下一轮又被匹配成 "@明"
      i += name.length
      break
    }
    if (slugs.length >= MAX_MENTIONS) break
  }
  return slugs
}

/**
 * 把「名字 → slug」表补齐（角色目录 + 本地常量表）
 *
 * 服务端 `/v1/characters` 是权威；本地 `constants/character` 那张表用于
 * 服务端拿不到时兜底（离线时 @ 仍然能插进文本，只是 slug 退回名字）。
 */
export function buildNameToSlug(
  ...maps: Array<Record<string, string> | undefined | null>
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const map of maps) {
    if (!map) continue
    for (const [name, slug] of Object.entries(map)) {
      if (name && slug) out[name] = slug
    }
  }
  return out
}
