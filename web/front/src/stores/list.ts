// =============================================================================
// 列表数据源（list store，P1 · 2026-11-18）
// -----------------------------------------------------------------------------
// 灰度开关：`baker_list_source = 'local' | 'server'`
//
//   local  —— 完全按改造前的行为：列表读这台设备的 IndexedDB（chatStore.cards）
//   server —— 列表读 `/v1`：角色目录 + 会话列表 → 拼成卡树（见 utils/conversationSync）
//
// 默认值不是写死 local，而是**按这台设备有没有旧数据决定**（2026-11-18 复验后修）：
//   · 没有旧本地数据（新设备 / 新账号）→ 默认 server：换设备登录、领取取件码
//     之后列表立刻可见，这正是 P1 要解决的事；
//   · 有旧本地数据 → 默认 local + 必须先迁移（`bakerai.legacy-migrated.v1`），
//     否则切过去会看起来像"数据全没了"。
//
// 三条硬规则：
//   1. **server 模式不写本地卡库**（见 useChatPersistence 的跳过逻辑），
//      老存档原样留着，切回 local 立刻恢复。
//   2. **断网不白屏**：列表读上次成功的缓存（localStorage），并亮"离线"提示。
//   3. **领取要看得见**：领完调 `afterClaim()` —— 该切 server 就切，然后刷新。
// =============================================================================

import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useChatStore } from './chat'
import { useAccountStore } from './account'
import { bindConversation } from '../utils/backend'
import {
  buildServerCards,
  fetchConversationMessages,
  fetchServerCharacters,
  fetchServerConversations,
  mergeServerCards,
  toChatMessage,
  type ServerCharacter,
  type ServerConversation,
  type ServerListCache,
} from '../utils/conversationSync'
import {
  canUseServerSource,
  pickInitialSource,
  planSetSource,
  type ListSource,
} from '../utils/listSource'
import { detectLegacyLocal, isLegacyMigrated } from '../utils/legacyLocalData'
import { uploadLocalNow } from '../utils/cloudSync'
import { devInfo, devWarn } from '../utils/logger'
import type { Card } from '../types/chat'

export type { ListSource }

/** 灰度开关：列表数据源（localStorage） */
export const LIST_SOURCE_KEY = 'baker_list_source'
/** 上次成功拉到的服务端列表（断网时用） */
export const LIST_CACHE_KEY = 'baker_server_list_cache'
/**
 * "这台设备有没有旧本地数据"的缓存（'1' / '0'）
 *
 * 真正的事实要读 IndexedDB（异步），而门槛判定要同步 —— 所以检测一次后缓存下来。
 * 检测逻辑本身仍在 fe-migrate 的 `detectLegacyLocal()` 里，本文件只调用、不复制。
 */
export const LEGACY_DATA_FLAG_KEY = 'baker_has_legacy_data'
/**
 * "用户**显式**选过数据源"的标记（'1'）
 *
 * 2026-11-18 修（审计 D5）：用户手动选了本机存档，登录后不能被自动改成服务端。
 * 这个标记只在设置页点选时写；程序自动挑的默认值不写。
 */
export const LIST_SOURCE_USER_SET_KEY = 'baker_list_source_user_set'

/** 读开关；没存过返回 null（"首次运行，等检测完再定"） */
function readStoredSource(): ListSource | null {
  try {
    const raw = localStorage.getItem(LIST_SOURCE_KEY)
    if (raw === 'server') return 'server'
    if (raw === 'local') return 'local'
    return null
  } catch {
    return null
  }
}

/** 写开关（静默失败：隐私模式下次仍按默认走） */
function writeStoredSource(value: ListSource): void {
  try {
    localStorage.setItem(LIST_SOURCE_KEY, value)
  } catch {
    // 忽略
  }
}

/** 读"有没有旧数据"的缓存 */
function readLegacyFlag(): boolean | null {
  try {
    const raw = localStorage.getItem(LEGACY_DATA_FLAG_KEY)
    if (raw === '1') return true
    if (raw === '0') return false
    return null
  } catch {
    return null
  }
}

function writeLegacyFlag(value: boolean): void {
  try {
    localStorage.setItem(LEGACY_DATA_FLAG_KEY, value ? '1' : '0')
  } catch {
    // 忽略
  }
}

/** 读缓存（坏了当没有） */
function readCache(): ServerListCache | null {
  try {
    const raw = localStorage.getItem(LIST_CACHE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as ServerListCache
    if (!parsed || !Array.isArray(parsed.conversations)) return null
    return parsed
  } catch {
    return null
  }
}

/** 写缓存（静默失败：缓存写不进去不影响在线使用） */
function writeCache(cache: ServerListCache): void {
  try {
    localStorage.setItem(LIST_CACHE_KEY, JSON.stringify(cache))
  } catch {
    // 隐私模式 / 配额满：忽略
  }
}

export const useListStore = defineStore('list', () => {
  const chatStore = useChatStore()

  // ---- 开关与门槛 -----------------------------------------------------------
  /** 用户选的（可能是 server，但被门槛挡住） */
  const requestedSource = ref<ListSource>(readStoredSource() ?? 'local')

  /** 用户是否已经明确选过数据源（没选过 = 首次运行，等检测完按默认值写一次） */
  const sourceChosen = ref<boolean>(readStoredSource() !== null)

  /** 用户是否**显式**点过数据源开关（显式选择永不被自动逻辑覆盖，见审计 D5） */
  const userSet = ref<boolean>(readUserSet())

  /** 最近一次"云端补齐/上传"的一句话结果（界面显示在设置页） */
  const syncNotice = ref('')

  /** 正在上传本机存档 */
  const uploading = ref(false)

  function readUserSet(): boolean {
    try {
      return localStorage.getItem(LIST_SOURCE_USER_SET_KEY) === '1'
    } catch {
      return false
    }
  }

  function writeUserSet(): void {
    try {
      localStorage.setItem(LIST_SOURCE_USER_SET_KEY, '1')
    } catch {
      // 忽略
    }
  }

  /** 这台设备有没有旧本地数据（null = 还没检测） */
  const hasLegacyData = ref<boolean | null>(readLegacyFlag())

  /** 迁移标记：`bakerai.legacy-migrated.v1` 为真时，有旧数据也允许 server */
  const migrated = ref<boolean>(isLegacyMigrated())

  /** 门槛状态（给纯函数用） */
  const gateState = computed(() => ({
    hasLegacyData: hasLegacyData.value,
    migrated: migrated.value,
  }))

  /** 实际生效的数据源：选了 server 但被门槛挡住 → 退回 local */
  const source = computed<ListSource>(() =>
    requestedSource.value === 'server' && canUseServerSource(gateState.value) ? 'server' : 'local',
  )

  /** 被门槛挡住时的说明（设置页直接显示这句） */
  const blockedReason = computed<string>(() => {
    if (requestedSource.value !== 'server') return ''
    if (canUseServerSource(gateState.value)) return ''
    return planSetSource('server', gateState.value).reason
  })

  // ---- 同步状态 -------------------------------------------------------------
  const loading = ref(false)
  const offline = ref(false)
  const error = ref('')
  const lastSyncedAt = ref(0)
  /** 当前列表是不是从缓存画出来的（离线 / 拉取失败时为 true，界面显示"缓存"） */
  const fromCache = ref(false)
  /**
   * 上一次"切换被门禁拒绝"的原因（只存在内存里，**不写 localStorage**）
   *
   * 被拒时不改持久化的选择，但界面得说清为什么没切过去 —— 否则用户点了没反应。
   */
  const rejectedReason = ref('')

  // ---- 旧数据检测（异步，只做一次） -----------------------------------------
  /**
   * 检测这台设备有没有旧本地数据，并缓存结果
   *
   * 用的是 fe-migrate 的 `detectLegacyLocal()`（只调用、不修改）。
   * 失败时按"没有旧数据"处理：检测本身失败说明读不到旧库，
   * 这时把新人挡在 local 里反而更糟（他本来就没有本地卡可看）。
   */
  async function detectLegacyOnce(): Promise<boolean | null> {
    if (hasLegacyData.value !== null) return hasLegacyData.value
    try {
      const detected = await detectLegacyLocal()
      hasLegacyData.value = !!detected?.hasData
    } catch (err) {
      devWarn('[list] 检测旧数据失败，按"没有旧数据"处理：', err)
      hasLegacyData.value = false
    }
    writeLegacyFlag(hasLegacyData.value)
    return hasLegacyData.value
  }

  // ---- 开关操作 -------------------------------------------------------------
  /**
   * 切换数据源
   *
   * **先判定、后写入**：被门槛拒绝时一个字节都不改 localStorage
   * （2026-11-18 修 verify-front FAIL 2：旧实现先写后拒，刷新后开关与实际不一致）。
   *
   * @returns 成功与否；失败时 `blockedReason` 里有原因
   */
  function setSource(next: ListSource, opts: { explicit?: boolean } = {}): boolean {
    // 用最新的迁移标记做判定：fe-migrate 刚写完标记时不必等下一次刷新
    migrated.value = isLegacyMigrated()
    const plan = planSetSource(next, gateState.value)
    if (!plan.ok) {
      // 被拒：**只记一句提示**，一个字节都不写 localStorage（2026-11-18 复验 FAIL 2）
      rejectedReason.value = plan.reason
      return false
    }
    rejectedReason.value = ''

    writeStoredSource(next)
    sourceChosen.value = true
    // 只有"用户自己点的"才算显式选择；登录/领取触发的自动切换不写这个标记
    if (opts.explicit !== false) {
      userSet.value = true
      writeUserSet()
    }

    // 【2026-11-20 本地优先】切到云端 = "再拉一份云端会话、把本机没有的补进来"，
    // **不再**整棵替换卡树，所以不需要先给本地拍快照、切回来再放回去
    // （以前那套 localSnapshot 是"替换式"时代的产物，已经删掉）。
    // 本机数据一刻都不会消失：合并只加不改（见 conversationSync.mergeServerCards）。
    requestedSource.value = next
    if (next === 'server') {
      void refresh()
      return true
    }
    return true
  }

  // ---- 列表刷新 -------------------------------------------------------------
  /**
   * 拉一次服务端列表，**合并**进本机卡树
   *
   * 2026-11-20 本地优先：以前是整棵替换（并把结果当"渲染缓存"不落盘），现在
   * 走 `mergeServerCards` —— 本机独有的会话一条不动，云端独有的补进来，
   * 补完照常写本地库（本地永远有完整副本）。
   *
   * - 成功：合并 + 写缓存 + 清掉离线标记
   * - 失败：`offline = true`，有缓存就用缓存补一次（不白屏），否则什么都不动
   *
   * ⚠ 不 await 的调用点很多（启动 / 登录 / 领取），所以这里**不抛错**。
   */
  async function refresh(): Promise<boolean> {
    if (source.value !== 'server') return false
    return pullAndMerge()
  }

  /**
   * 拉云端 → 合并进本机（不看数据源开关）
   *
   * `refresh()` 与启动补齐、登录补齐都走这一个实现：三处行为必须一致，
   * 否则又会出现"手动切过去有数据、自动进来没有"这种两边分叉。
   */
  async function pullAndMerge(): Promise<boolean> {
    loading.value = true
    error.value = ''
    try {
      const [characters, conversations] = await Promise.all([
        fetchServerCharacters(),
        fetchServerConversations(),
      ])
      const serverCards = buildServerCards(conversations, characters)
      const merged = mergeServerCards(chatStore.cards as Card[], serverCards)
      const result = chatStore.applyMergedList(merged)
      writeCache({ at: Date.now(), characters, conversations })
      lastSyncedAt.value = Date.now()
      offline.value = false
      fromCache.value = false
      if (result.addedCards > 0 || result.addedConvs > 0) {
        devInfo(
          '[list] 云端补齐',
          `新增卡 ${result.addedCards} / 会话 ${result.addedConvs}`,
        )
        syncNotice.value = `已从云端补进来 ${result.addedConvs} 段会话`
      }
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      error.value = message
      offline.value = true
      devWarn('[list] 拉服务端列表失败：', err)
      // 有缓存就用缓存补一遍（用户至少能看到上次的列表，而不是空白）
      const cache = readCache()
      if (cache) applyCache(cache)
      return false
    } finally {
      loading.value = false
    }
  }

  /** 用缓存补列表（离线用；同样是合并，不覆盖本机） */
  function applyCache(cache: ServerListCache): void {
    const characters: ServerCharacter[] = cache.characters ?? []
    const conversations: ServerConversation[] = cache.conversations ?? []
    const serverCards = buildServerCards(conversations, characters)
    chatStore.applyMergedList(mergeServerCards(chatStore.cards as Card[], serverCards))
    lastSyncedAt.value = cache.at || 0
    fromCache.value = true
  }

  /** 本机现在有几段会话（判断"要不要从云端补"用） */
  function localConversationCount(): number {
    return (chatStore.cards as Card[]).reduce((n, c) => n + (c.conversations?.length ?? 0), 0)
  }

  /**
   * 本机有几段**真的聊过**的会话（有消息的）
   *
   * 为什么不用上面那个总数：cards 恒含几十张内置角色卡，每张都挂一段空的占位对话，
   * 总数永远 > 0 —— 拿它判断"本机是不是空的"，等于这条补齐永远不触发
   * （2026-11-20 实测：新设备登录后看不到云端记录，就是这个原因）。
   */
  function localChattedCount(): number {
    return (chatStore.cards as Card[]).reduce(
      (n, c) => n + (c.conversations ?? []).filter((conv) => (conv.messages?.length ?? 0) > 0).length,
      0,
    )
  }

  /**
   * 本机是空的时候，从云端补一次
   *
   * 给谁用：新设备 / 换浏览器 / 清过缓存，但账号（或设备身份）名下还有记录的人。
   * 以前他们的默认数据源会被判成 server，现在默认读本机 → 不补的话打开是空的。
   *
   * 只在"本机一段聊过的会话都没有"时跑，所以不需要额外的"补过了"标记：
   * 补进来之后本机就有会话了，下次启动条件自然不成立。
   */
  async function hydrateIfEmpty(): Promise<boolean> {
    if (localChattedCount() > 0) return false
    return pullAndMerge()
  }

  /**
   * 启动时调一次
   *
   * 顺序很关键：
   *   1. 先检测这台设备有没有旧数据（异步，只做一次）
   *   2. 首次运行 → 写默认开关（**永远是 local**，本地优先）
   *   3. 用户显式选了云端 → 拉一次合并；否则本机为空时补一次（新设备恢复）
   */
  async function ensureLoaded(): Promise<void> {
    await detectLegacyOnce()

    if (!sourceChosen.value) {
      const initial = pickInitialSource(hasLegacyData.value)
      writeStoredSource(initial)
      requestedSource.value = initial
      sourceChosen.value = true
    }

    if (source.value === 'server') {
      if (lastSyncedAt.value > 0) return
      await refresh()
      return
    }
    // 本机存档优先：只有"本机什么都没有"时才去云端捞一次（新设备 / 清过缓存）
    await hydrateIfEmpty()
  }

  /**
   * 领取取件码 / 导入数据包之后调这个
   *
   * 领取的结果是"服务端多了一批会话"，所以动作就是**从云端补进本机**
   * （2026-11-20 起不再需要切数据源：补进来之后本机就读得到）。
   */
  async function afterClaim(): Promise<void> {
    await detectLegacyOnce()
    migrated.value = isLegacyMigrated()
    await pullAndMerge()
  }

  /**
   * 登录后自动上传本机存档（2026-11-20 新增）
   *
   * 为什么不是"每次聊天都传"：聊天本身就走服务端接口，云端本来就跟着更新；
   * 真正需要传的只有"以前攒在本机、服务端还没有"的那批老记录。
   * 而 `POST /v1/imports` 只按包内容去重，重复整包上传会在云端建重复会话，
   * 所以按账号只传一次（见 utils/cloudSync 的注释）。
   *
   * @param force 设置页「重新上传」用
   */
  async function uploadLocalNow_(options: { force?: boolean } = {}): Promise<{
    uploaded: boolean
    reason: string
    conversations: number
    messages: number
  }> {
    const account = useAccountStore()
    const userId = String(account.user?.id ?? '')
    if (uploading.value) {
      return { uploaded: false, reason: '正在上传，请稍候', conversations: 0, messages: 0 }
    }
    uploading.value = true
    try {
      const result = await uploadLocalNow({
        userId,
        force: options.force,
        onProgress: (text) => { syncNotice.value = text },
      })
      if (result.uploaded) {
        syncNotice.value = `已上传本机存档：${result.conversations} 段会话 / ${result.messages} 条消息`
      } else if (result.reason) {
        syncNotice.value = result.reason
      }
      return result
    } finally {
      uploading.value = false
    }
  }

  /**
   * 登录这一刻要做的事（本地优先版）
   *
   *   1. 把本机存档传上云（这个账号第一次登录时才真传）
   *   2. 把云端独有的会话补进本机（换设备的人立刻看到自己的记录）
   *   3. 如果用户显式选了云端，再刷一次列表（与手动刷新同一条实现）
   */
  async function onLoggedIn(): Promise<void> {
    syncNotice.value = ''
    await uploadLocalNow_()
    const hydrated = await hydrateIfEmpty()
    if (!hydrated && source.value === 'server') await pullAndMerge()
  }

  // ---- 单条会话：拉历史 -----------------------------------------------------
  /**
   * 确保某段对话的服务端历史已经拉过一次
   *
   * 点进子卡时调（App 监听 activeSub）。已经拉过就不再打网络，
   * 除非 `force`（重新生成 / 手动刷新时用）。
   */
  async function ensureMessages(sub: number, force = false): Promise<boolean> {
    if (source.value !== 'server') return false
    const conv = chatStore.conversations[sub]
    if (!conv?.remoteId) return false
    if (conv.remoteLoaded && !force) return true

    const remoteId = conv.remoteId
    try {
      const { messages } = await fetchConversationMessages(remoteId, conv.remoteNextSeq ?? 0)
      const ok = chatStore.upsertRemoteMessages(sub, messages.map(toChatMessage), remoteId)
      if (ok) conv.remoteLoaded = true
      offline.value = false
      return ok
    } catch (err) {
      devWarn('[list] 拉会话消息失败：', err)
      error.value = err instanceof Error ? err.message : String(err)
      offline.value = true
      return false
    }
  }

  /**
   * 选中的会话变了：拉历史 + 把「角色 → 会话」绑定写对
   *
   * 为什么要写绑定：发送链路（utils/backend.ts 的 ensureConversation）仍然按
   * 「角色 slug → 会话 id」找会话。P1 不改这条链路，只在**选中服务端会话时**
   * 把绑定指向这条会话 —— 于是"发消息发到哪一段"由列表的点击决定，
   * 而不是靠猜（docs/05 §2 的老毛病就是这么消掉的）。
   */
  function onActiveSubChanged(sub: number | null): void {
    if (source.value !== 'server' || sub === null) return
    const conv = chatStore.conversations[sub]
    if (!conv?.remoteId) return
    if (conv.remoteCharacterId) {
      // 键要跟发送链路用的一致（见 utils/localConversation.conversationKeyOf），
      // 否则绑定写在一个键上、发送去找另一个键 → 会自己再建一条会话。
      bindConversation(conv.remoteCharacterId, conv.remoteId, `r:${conv.remoteId}`)
    }
    // 群聊卡：groupSessionId 已经在卡上，群聊链路自己会读（见 groupBackend）
    void ensureMessages(sub)
  }

  /** 迁移标记变了（fe-migrate 迁移成功后 / 设置页打开时）→ 重新读一次 */
  function refreshMigratedFlag(): void {
    migrated.value = isLegacyMigrated()
  }

  // ---- 登录 / 注册后：自动上传本机存档 + 云端补齐 ---------------------------
  // 2026-11-20 本地优先：登录不再"换列表视图"（列表永远读本机），而是
  // "把本机存档传上云 + 把这台设备还没有的云端记录补进来"。
  //
  // 监听的是 `loginSeq`（真的登录/注册成功）而不是 `state`：
  // 启动时 refresh() 也会把状态恢复成 user，那不是"用户刚登录" ——
  // 用状态变化当信号的话，每次开机都会白读一遍整个卡库。
  const accountStore = useAccountStore()
  watch(
    () => accountStore.loginSeq,
    (seq, prev) => {
      if (seq <= prev) return
      void (async () => {
        await detectLegacyOnce()
        await onLoggedIn()
      })()
    },
  )

  return {
    // 状态
    requestedSource,
    source,
    hasLegacyData,
    migrated,
    blockedReason,
    loading,
    offline,
    error,
    lastSyncedAt,
    fromCache,
    rejectedReason,
    userSet,
    syncNotice,
    uploading,
    // 操作
    setSource,
    refresh,
    afterClaim,
    ensureLoaded,
    ensureMessages,
    onActiveSubChanged,
    refreshMigratedFlag,
    detectLegacyOnce,
    // 本地优先（2026-11-20）
    hydrateIfEmpty,
    onLoggedIn,
    uploadLocalNow: uploadLocalNow_,
    localConversationCount,
  }
})
