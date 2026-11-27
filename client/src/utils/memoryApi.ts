// =============================================================================
// 干员记忆（memoryApi）—— 跨对话「人情账」的请求层
// -----------------------------------------------------------------------------
// 后端：`web/back/app/api/memories.py`，五个端点：
//
//   GET    /v1/memories?character_id=<slug>   某个干员记着你什么
//   DELETE /v1/memories/{id}                  删一条（软删 + 留指纹，不会复活）
//   PATCH  /v1/memories/{id}                  改文本 / 置顶（本文件暂未用到）
//   GET    /v1/memories/settings              全局开关 {mode:'on'|'off'}
//   PUT    /v1/memories/settings              切全局开关
//
// ⚠ 两个必须记住的后端事实（决定了本文件怎么写）：
//
//   1. **列表必须带 character_id**。后端没有「一次返回全部干员」的端点，
//      所以「按干员分组」只能先把角色目录拉下来，再逐个干员查一次
//      （36 个角色 ≈ 36 个小请求）。本文件用 6 路并发 + 单次展开只查一遍，
//      把这件事限制在「用户点开「她记得什么」」那一刻。
//   2. **没有「改已有会话」的接口**。会话级的 memory_mode 只在
//      `POST /v1/conversations` 建会话时写进去，之后改不了。
//      所以本文件的 `readNewChatMemory()/writeNewChatMemory()` 只是一个
//      **本地偏好**：在「下一次建会话」时把值交给 memory 字段（见 utils/backend.ts
//      与 utils/groupBackend.ts）。
//
// 这一层只做「请求 + 形状归一」，不碰 Vue / Pinia：面板组件和自测脚本都能直接用。
// =============================================================================

import { apiFetch, readError } from './apiClient'
import { MEMORY_ENABLED } from '../constants/features'
import { fetchServerCharacters } from './conversationSync'

/** 全局开关（后端 `memory_settings.mode`） */
export type MemoryMode = 'on' | 'off'

/**
 * 建会话时的记忆选择（后端 `conversations.memory_mode`）
 *
 * - inherit：跟随全局默认（后端默认值）
 * - on / off：这一局单独覆盖
 */
export type MemoryNewChat = 'inherit' | 'on' | 'off'

/** 一条记忆（后端 `MemoryOut`） */
export interface MemoryItem {
  id: number
  kind: string
  text: string
  pinned: boolean
  /** ISO 时间（后端给 UTC，展示时转本地） */
  created_at: string
  updated_at: string
}

/** 一个干员 + 她记着的条目 */
export interface MemoryGroup {
  /** 角色 slug（后端 `character_id`） */
  id: string
  /** 角色中文名（角色目录里的 name；查不到就退回 slug） */
  name: string
  items: MemoryItem[]
}

/** 一次「全部干员」快照 */
export interface MemorySnapshot {
  /** 全局开关（顺手从列表接口带回，避免多一次请求） */
  mode: MemoryMode
  groups: MemoryGroup[]
  total: number
  /** 个别干员没读到（网络抖动）时 > 0，面板给一句提示，不整块报错 */
  failures: number
}

/**
 * kind 的中文标签（后端是封闭枚举，见 docs/04-干员记忆.md §4.3）
 *
 * 后端将来加 kind 时这里会显示原始英文；那也比赛一个错的中文好。
 */
export const MEMORY_KIND_LABELS: Record<string, string> = {
  nickname: '称呼',
  call: '叫法',
  appearance: '样子',
  relation: '关系',
  preference: '偏好',
  dislike: '讨厌',
  promise: '约定',
  fact: '事实',
}

/** kind → 中文标签（未知 kind 原样显示） */
export function memoryKindLabel(kind: string): string {
  return MEMORY_KIND_LABELS[kind] ?? kind ?? '其他'
}

/**
 * 新建会话时默认用哪种记忆
 *
 * 存 localStorage（纯本地偏好，跟着这台设备走）：
 *   'baker_memory_new_chat' = 'inherit' | 'on' | 'off'
 *
 * 为什么不做进 settings store：它只影响「建会话」这一条链路，
 * 而且要在 utils/backend.ts（不依赖 Pinia 的纯请求层）里读 —— 直接读
 * localStorage 比给请求层塞一个 store 依赖干净得多。
 */
export const NEW_CHAT_MEMORY_KEY = 'baker_memory_new_chat'

/** 读「新建会话时的记忆选择」（没存过 / 存储被禁用 → inherit） */
export function readNewChatMemory(): MemoryNewChat {
  // 4.0.5：干员记忆暂时不对用户开放 —— 新会话一律按 off 建，
  // 后端因此不会写关于用户的新记忆，也不会把旧记忆塞进提示词。
  // 本地存的旧偏好不去动它（翻回 MEMORY_ENABLED = true 时原样生效）。
  if (!MEMORY_ENABLED) return 'off'
  try {
    const raw = localStorage.getItem(NEW_CHAT_MEMORY_KEY)
    if (raw === 'on' || raw === 'off' || raw === 'inherit') return raw
  } catch {
    // 隐私模式 / 存储被禁用：按默认处理
  }
  return 'inherit'
}

/** 写「新建会话时的记忆选择」（写失败静默：下次仍按默认 inherit） */
export function writeNewChatMemory(value: MemoryNewChat): void {
  // 功能关闭期间不接受写入：否则用户（或旧面板残留）改到的值会在
  // 功能恢复的那一刻突然生效，等于绕过开关。
  if (!MEMORY_ENABLED) return
  try {
    localStorage.setItem(NEW_CHAT_MEMORY_KEY, value)
  } catch {
    // 静默失败
  }
}

/** 读全局开关 */
export async function fetchMemorySettings(signal?: AbortSignal): Promise<MemoryMode> {
  const response = await apiFetch('/memories/settings', { method: 'GET', signal })
  if (!response.ok) throw await readError(response)
  const data = (await response.json()) as { mode?: string }
  return data.mode === 'off' ? 'off' : 'on'
}

/** 切全局开关（后端返回生效后的值，以它为准） */
export async function saveMemorySettings(
  mode: MemoryMode,
  signal?: AbortSignal,
): Promise<MemoryMode> {
  const response = await apiFetch('/memories/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
    signal,
  })
  if (!response.ok) throw await readError(response)
  const data = (await response.json()) as { mode?: string }
  return data.mode === 'off' ? 'off' : 'on'
}

/** 某个干员记着你什么（后端 `MemoryListOut`） */
export async function fetchMemoriesFor(
  characterId: string,
  signal?: AbortSignal,
): Promise<{ mode: MemoryMode; items: MemoryItem[]; total: number; limit: number }> {
  const query = `?character_id=${encodeURIComponent(characterId)}`
  const response = await apiFetch(`/memories${query}`, { method: 'GET', signal })
  if (!response.ok) throw await readError(response)

  const data = (await response.json()) as {
    mode?: string
    items?: Partial<MemoryItem>[]
    total?: number
    limit?: number
  }

  // 逐条归一：后端字段缺了不该让面板崩（宁可少显示一条，也不整块白屏）
  const items: MemoryItem[] = (data.items ?? [])
    .filter((item) => !!item && typeof item.id === 'number')
    .map((item) => ({
      id: item.id as number,
      kind: typeof item.kind === 'string' ? item.kind : '',
      text: typeof item.text === 'string' ? item.text : '',
      pinned: item.pinned === true,
      created_at: typeof item.created_at === 'string' ? item.created_at : '',
      updated_at: typeof item.updated_at === 'string' ? item.updated_at : '',
    }))

  return {
    mode: data.mode === 'off' ? 'off' : 'on',
    items,
    total: typeof data.total === 'number' ? data.total : items.length,
    limit: typeof data.limit === 'number' ? data.limit : 40,
  }
}

/** 删一条（成功 204；失败抛 ApiError，404 = 这条已经没了） */
export async function deleteMemory(memoryId: number, signal?: AbortSignal): Promise<void> {
  const response = await apiFetch(`/memories/${memoryId}`, { method: 'DELETE', signal })
  if (!response.ok) throw await readError(response)
}

/** 同时查几个干员（后端没有聚合端点，太多会排队；6 路够快也不至于打暴后端） */
const CHAR_CONCURRENCY = 6

/**
 * 拉「全部干员」的记忆，按角色目录顺序分组
 *
 * 流程：`GET /v1/characters`（1 次）→ 对每个 slug 查 `GET /v1/memories`（N 次）。
 * 只保留有条目的干员：面板要的是「她记得什么」，没记过的干员不占位置。
 *
 * 个别干员失败不整块报错（failures 计数返回给界面提示），全部失败才抛错 ——
 * 否则网络抖一下就把「她记得什么」整页变成报错，比少显示两个干员更糟。
 */
export async function fetchAllMemories(signal?: AbortSignal): Promise<MemorySnapshot> {
  const characters = await fetchServerCharacters()
  const order = new Map<string, number>()
  characters.forEach((c, index) => order.set(c.id, index))

  const groups: MemoryGroup[] = []
  let mode: MemoryMode = 'on'
  let modeSeen = false
  let failures = 0
  let firstError: unknown = null
  let cursor = 0

  async function worker(): Promise<void> {
    while (cursor < characters.length) {
      const character = characters[cursor]
      cursor += 1
      if (!character) continue
      try {
        const data = await fetchMemoriesFor(character.id, signal)
        if (!modeSeen) {
          mode = data.mode
          modeSeen = true
        }
        if (data.items.length > 0) {
          groups.push({
            id: character.id,
            name: character.name || character.id,
            items: data.items,
          })
        }
      } catch (err) {
        // 用户主动取消：直接退出，别把取消算成"读失败"
        if (err instanceof DOMException && err.name === 'AbortError') throw err
        failures += 1
        if (firstError === null) firstError = err
      }
    }
  }

  const lanes = Math.min(CHAR_CONCURRENCY, Math.max(1, characters.length))
  await Promise.all(Array.from({ length: lanes }, () => worker()))

  if (characters.length > 0 && failures === characters.length && firstError) {
    throw firstError
  }

  // 后端并发返回顺序是乱的：按角色目录顺序排回来，界面每次展开都一样
  groups.sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))

  const total = groups.reduce((sum, group) => sum + group.items.length, 0)
  return { mode, groups, total, failures }
}

/**
 * 时间戳 → 本地「YYYY-MM-DD HH:mm」
 *
 * 后端给的是带时区的 ISO（UTC）。`toLocaleString` 在不同 WebView 上格式不一，
 * 这里自己拼，保证三端看起来一样；解析失败时原样返回（宁可显示怪字符串，
 * 也不要显示 "Invalid Date"）。
 */
export function formatMemoryTime(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}
