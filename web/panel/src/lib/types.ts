/**
 * types.ts —— 后端接口的形状，一处定义。
 *
 * 全部照 `web/back/app/api/admin.py` 抄，**不自创字段**：后台面板最怕的就是
 * "前端以为有、后端其实没给"，那种错在运行时才炸，而且只炸在某个数字上。
 * 所以这里每个可选字段都写清楚（`?`），页面必须自己兜底。
 */

/** `GET /v1/admin/overview` → counts：全站规模 */
export interface OverviewCounts {
  users: number
  devices: number
  conversations: number
  messages: number
  characters: number
  /** 世界书条目数（后端多给的一个数，首页当次要指标） */
  worldbook: number
}

/** overview → keys：密钥池健康度 */
export interface KeyStats {
  total: number
  enabled: number
  /** 启用中但正在冷却 */
  cooldown: number
  disabled: number
}

/** 用量汇总：今日 / 某一天 / 整段时间都长这样 */
export interface UsageTotals {
  requests: number
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
  avg_ms: number
}

/** 按天的一格。`date` 是 `YYYY-MM-DD`（**东八区日**：后端把 UTC 时间 +8 小时再 date()） */
export interface UsageDay extends UsageTotals {
  date: string
}

/** `GET /v1/admin/overview` */
export interface Overview {
  counts: OverviewCounts
  keys: KeyStats
  today: UsageTotals
  days: UsageDay[]
  /** 服务器当前时间（ISO UTC），用来算"最近 7 天"的窗口 */
  now: string
}

/** 后台看到的账号 */
export interface AdminUser {
  id: string
  username: string
  display_name: string
  status: 'active' | 'disabled'
  role: 'user' | 'admin'
  devices: number
  created_at: string
  last_seen_at: string | null
}

/** `GET /v1/admin/users` */
export interface UserPage {
  total: number
  limit: number
  offset: number
  items: AdminUser[]
}

/** 可改的三个字段（PATCH 只传要改的） */
export interface UserPatch {
  status?: 'active' | 'disabled'
  role?: 'user' | 'admin'
  display_name?: string
}

export interface AdminCharacter {
  /** slug，不是自增 id */
  id: string
  name: string
  avatar: string
  attribute: string
  enabled: boolean
  /** 发言权重 1–100 */
  active_weight: number
  sort_order: number
  /** 当前启用的提示词版本号；0 = 没有提示词 */
  prompt_ver: number
}

export interface CharacterPatch {
  enabled?: boolean
  name?: string
  active_weight?: number
  sort_order?: number
}

export interface ConfigItem {
  key: string
  value: string
  updated_at: string
}

export interface AdminKey {
  id: number
  label: string
  enabled: boolean
  /** 此刻是否在冷却（后端算好给的布尔值） */
  cooling: boolean
  cooldown_until: string | null
  fail_count: number
  /** 每分钟上限 */
  rpm: number
  base_url: string
  model: string
  last_used_at: string | null
  /** 注意：**永远不会**有 key 明文/密文字段，后端连密文都不返回 */
}

export interface ReasoningItem {
  seq: number
  speaker: string
  reply: string
  reasoning: string
  created_at: string
}

export interface ReasoningResult {
  conversation_id: string
  items: ReasoningItem[]
}

/** `POST /v1/auth/login` 的返回。**没有 role 字段** —— 权限靠打一次后台接口试出来 */
export interface LoginResult {
  user: { id: string; username: string; display_name: string; created_at: string }
  adopted_conversations: number
}
