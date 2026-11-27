// =============================================================================
// legacyLocalData —— 读老网页版/老客户端留在浏览器本地的数据，并迁移到新后端
// -----------------------------------------------------------------------------
// 主路径（用户拍板：新 web 直接覆盖原 web，同一个 origin）：
//   浏览器的 IndexedDB / localStorage 是按 **origin** 隔离的。只要新前端部署在
//   老站的那个 origin 上（不管域名是 test2.peilika.beer 还是 chat.peilika.beer），
//   打开的是同一个 `endfield-baker` 库 —— 所以这里**不写死任何域名**，直接用
//   当前页面的 origin 读。反过来：如果新站换了 origin，浏览器会当成另一个网站，
//   这份数据我们读不到（不是权限问题，是浏览器隔离），只能走"导出文件再导入"。
//
// 这个文件只做"浏览器胶水"：
//   · 打开老库、按老键名读（含 3.2.2 的分片格式）
//   · 读老 localStorage 兜底（提示词覆盖、性别）
//   · 打包端（Electron/APK）读同一个 `endfield-baker-data.json`
//   · 调 core 里的纯函数拼包 / 上传 / 写标记
//
// 硬规矩（写在这里，改代码前先读）：
//   1. **只读**。不 deleteDatabase、不 localStorage.removeItem、不 put 老 key。
//   2. 唯一会写的本地东西是**新 key**：`bakerai.legacy-migrated.v1`（迁移标记）
//      和 `bakerai.legacy-sw-cleaned.v1`（老 SW 清理标记）。
//   3. 迁移失败不写标记，可以无限重试；成功也不删老数据（用户可能还要回滚）。
// =============================================================================

import { v1Url } from '../constants/apiRoutes'
import { ensureDeviceToken, forgetDeviceToken } from './apiClient'
import { devInfo, devWarn } from './logger'
import { forgetAllBindings } from './backend'
import { getNativeFileBridge } from './nativeStorage'
import {
  LEGACY_CARDS_KEY,
  LEGACY_CARD_INDEX_KEY,
  LEGACY_CARD_SLOT_PREFIX,
  LEGACY_DB_NAME,
  LEGACY_DB_NAME_PREFIX,
  LEGACY_MY_GENDER_KEY,
  LEGACY_NATIVE_FILE,
  LEGACY_SETTINGS_KEY,
  LEGACY_STORE_NAME,
  MIGRATE_MARKER_KEY,
  SW_CLEAN_MARKER_KEY,
  buildImportPackage,
  countLegacyCards,
  isRecord,
  mergeLegacyCards,
  parseLegacyNativeJson,
  readMigrateMarker,
  runMigration,
  type LegacyCard,
  type LegacyDetectSummary,
  type LegacyLocalData,
  type MarkerStorage,
  type MigrationOutcome,
  type MigrationProgress,
} from './legacyLocalCore'

export {
  LEGACY_DB_NAME,
  LEGACY_STORE_NAME,
  MIGRATE_MARKER_KEY,
  buildImportPackage,
  countLegacyCards,
  describeMigration,
  describeMigrationError,
  isSupportedDataUrlImage,
  mergeLegacyCards,
  readMigrateMarker,
  splitImportPackage,
} from './legacyLocalCore'
export type {
  LegacyCard,
  LegacyDetectSummary,
  LegacyLocalData,
  MigrationOutcome,
  MigrationProgress,
} from './legacyLocalCore'

/** 老 localStorage 里的提示词覆盖（IndexedDB 的 settings 快照缺失时用它兜底） */
const LEGACY_PROMPT_OVERRIDES_LS_KEY = 'endfield-baker-settings-prompt-overrides'

// ---------------------------------------------------------------------------
// localStorage 安全访问
// ---------------------------------------------------------------------------

/** 拿到可写标记的 localStorage；隐私模式/被禁用时返回 null（迁移照样能跑，只是不记标记） */
function markerStorage(): MarkerStorage | null {
  try {
    const storage = window.localStorage
    // 只做一次"读得到"的探测。刻意不写探针再删：本模块对老数据只读，
    // 连 removeItem 都不出现（写标记失败由 writeMigrateMarker 的 try/catch 兜住）。
    storage.getItem(MIGRATE_MARKER_KEY)
    return storage
  } catch {
    return null
  }
}

function readLocalStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// IndexedDB：读老库（只读，绝不建表/改结构）
// ---------------------------------------------------------------------------

/**
 * 打开老库
 *
 * 为什么不在 `onupgradeneeded` 里建表：这个函数是**读取器**。如果库不存在，
 * 打开会顺手创建一个空库；这里建好 store 再关掉，至少不会留下"有库没有表"
 * 的坏状态（那种库会让后续正式写入一直失败）。读完返回 null 表示"没有老数据"。
 */
async function openLegacyDb(name: string): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return null
  // 先问浏览器"这个库存在吗"：存在才打开。光靠 open() 会在库不存在时**顺手建一个**，
  // 而检测器不该有任何写副作用（虽然空库无害，但"只读"就该真的只读）。
  // databases() 不支持的浏览器才退回到 open（见下面的 created 兜底）。
  try {
    const databases = await indexedDB.databases?.()
    if (Array.isArray(databases) && !databases.some((item) => item.name === name)) return null
  } catch {
    // databases() 不可用：继续走 open
  }
  return new Promise((resolve) => {
    let created = false
    let request: IDBOpenDBRequest
    try {
      request = indexedDB.open(name)
    } catch (err) {
      devWarn('[legacy] 打开老库失败', name, err)
      resolve(null)
      return
    }
    request.onupgradeneeded = () => {
      // 没有这个库（首次访问新站）：建一个空 store 保持一致，然后当作"没有老数据"
      created = true
      const db = request.result
      if (!db.objectStoreNames.contains(LEGACY_STORE_NAME)) db.createObjectStore(LEGACY_STORE_NAME)
    }
    request.onsuccess = () => {
      const db = request.result
      if (created || !db.objectStoreNames.contains(LEGACY_STORE_NAME)) {
        db.close()
        resolve(null)
        return
      }
      resolve(db)
    }
    request.onerror = () => {
      devWarn('[legacy] 打开老库失败', name, request.error)
      resolve(null)
    }
    request.onblocked = () => {
      // 老页面还开着（同一个库被别的连接升级/占用）：本次不迁移，下次再说
      devWarn('[legacy] 老库被占用（可能老页面还开着）', name)
      resolve(null)
    }
  })
}

/**
 * 当前 origin 下，所有"像本应用"的 IndexedDB 名字
 *
 * 两条线（web31 / 3.2.2）用的都是 `endfield-baker`，所以正常只会有一个。这里按
 * 前缀多收几个（`endfield-baker-*`）是为了：以后某条线若改名，读取器不至于
 * 只认死名字而漏掉数据。**只在同一个 origin 里找** —— 跨域名是浏览器隔离，
 * 读不到也不该去猜。
 */
async function listLegacyDbNames(): Promise<string[]> {
  const names = new Set<string>([LEGACY_DB_NAME])
  try {
    const databases = await indexedDB.databases?.()
    for (const item of databases ?? []) {
      const name = item?.name
      if (typeof name === 'string' && name.startsWith(LEGACY_DB_NAME_PREFIX)) names.add(name)
    }
  } catch {
    // databases() 不被支持（老浏览器）：只用固定库名
  }
  return [...names]
}

/** 一个只读事务里批量取若干 key；不存在的 key 返回 undefined */
function readKeys(db: IDBDatabase, keys: string[]): Promise<Map<string, unknown>> {
  return new Promise((resolve, reject) => {
    const out = new Map<string, unknown>()
    let tx: IDBTransaction
    try {
      tx = db.transaction(LEGACY_STORE_NAME, 'readonly')
    } catch (err) {
      reject(err)
      return
    }
    const store = tx.objectStore(LEGACY_STORE_NAME)
    for (const key of keys) {
      const req = store.get(key)
      req.onsuccess = () => out.set(key, req.result)
    }
    tx.oncomplete = () => resolve(out)
    tx.onerror = () => reject(tx.error ?? new Error('读取失败'))
    tx.onabort = () => reject(tx.error ?? new Error('读取中止'))
  })
}

interface DbSnapshot {
  /** 分片格式读出来的卡片（cardsV2 + card:N）；没有分片就是 null */
  shardCards: unknown[] | null
  /** 旧整库 `cards` 键；没有就是 null */
  legacyCards: unknown[] | null
  settings: Record<string, unknown> | null
  myGender: 'male' | 'female' | null
}

/**
 * 从老 IndexedDB 读一份完整快照
 *
 * 两个卡片来源都要读：
 *   · 3.2.2 起是 `cardsV2` 索引 + `card:<slot>` 分片，**新消息只写这里**
 *   · `cards` 是 3.2.2 迁移前/迁移那一刻的整库快照，之后不再更新（只作为回退）
 * 只读其中一个都会漏消息，所以两个都拿，交给 core 合并。
 */
async function readDbSnapshot(name: string): Promise<DbSnapshot | null> {
  const db = await openLegacyDb(name)
  if (!db) return null
  try {
    // 第一趟：索引 + 旧整库 + 设置
    const first = await readKeys(db, [
      LEGACY_CARDS_KEY,
      LEGACY_CARD_INDEX_KEY,
      LEGACY_SETTINGS_KEY,
      LEGACY_MY_GENDER_KEY,
    ])
    const legacyRaw = first.get(LEGACY_CARDS_KEY)
    const legacyCards = Array.isArray(legacyRaw) ? legacyRaw : null
    const indexRaw = first.get(LEGACY_CARD_INDEX_KEY)

    let shardCards: unknown[] | null = null
    if (isRecord(indexRaw) && indexRaw.v === 2 && Array.isArray(indexRaw.slots)) {
      const slots = indexRaw.slots.filter(
        (slot): slot is number => typeof slot === 'number' && Number.isFinite(slot),
      )
      if (slots.length > 0) {
        // 第二趟：按索引顺序读分片。缺一片就少一张卡，但绝不影响其他卡
        const shardMap = await readKeys(
          db,
          slots.map((slot) => `${LEGACY_CARD_SLOT_PREFIX}${slot}`),
        )
        shardCards = []
        for (const slot of slots) {
          const card = shardMap.get(`${LEGACY_CARD_SLOT_PREFIX}${slot}`)
          if (card !== undefined && card !== null) shardCards.push(card)
        }
      }
    }

    const settingsRaw = first.get(LEGACY_SETTINGS_KEY)
    const settings = isRecord(settingsRaw) ? settingsRaw : null
    const gender = first.get(LEGACY_MY_GENDER_KEY)
    const myGender = gender === 'female' ? 'female' : gender === 'male' ? 'male' : null
    return { shardCards, legacyCards, settings, myGender }
  } catch (err) {
    devWarn('[legacy] 读取老库失败', name, err)
    return null
  } finally {
    db.close()
  }
}

/** 把当前 origin 下所有"像本应用"的库都读一遍（正常只有一个） */
async function readAllDbSnapshots(): Promise<DbSnapshot[]> {
  const names = await listLegacyDbNames()
  const snapshots: DbSnapshot[] = []
  for (const name of names) {
    const snapshot = await readDbSnapshot(name)
    if (snapshot) snapshots.push(snapshot)
  }
  return snapshots
}

// ---------------------------------------------------------------------------
// 打包端（Electron / APK）：同一个 JSON 文件、同一套键名
// ---------------------------------------------------------------------------

/**
 * 读打包端的整库 JSON
 *
 * 老客户端（EXE/APK）的 `nativeStorage` 把**同一批键名**存进
 * `endfield-baker-data.json`（见 v322kit/src/utils/nativeStorage.ts），所以字段
 * 形状与网页 IndexedDB 完全一致 —— 这里的合并/转换逻辑两种端共用。
 * 网页端拿不到原生桥，这个函数返回 null，逻辑自动落在 IndexedDB 上。
 */
async function readNativeSnapshot(): Promise<DbSnapshot | null> {
  try {
    const bridge = await getNativeFileBridge()
    if (!bridge) return null
    const text = await bridge.readFile(LEGACY_NATIVE_FILE)
    if (!text) return null
    const parsed: unknown = JSON.parse(text)
    if (!isRecord(parsed)) return null
    const cards = parsed[LEGACY_CARDS_KEY]
    const settings = isRecord(parsed[LEGACY_SETTINGS_KEY])
      ? (parsed[LEGACY_SETTINGS_KEY] as Record<string, unknown>)
      : null
    const gender = parsed[LEGACY_MY_GENDER_KEY]
    return {
      shardCards: null, // 打包端从没用过分片格式（写文件时整库一个键）
      legacyCards: Array.isArray(cards) ? cards : null,
      settings,
      myGender: gender === 'female' ? 'female' : gender === 'male' ? 'male' : null,
    }
  } catch (err) {
    devWarn('[legacy] 读取打包端数据文件失败', err)
    return null
  }
}

// ---------------------------------------------------------------------------
// 组装：快照 → LegacyLocalData
// ---------------------------------------------------------------------------

function readPromptOverrides(settings: Record<string, unknown> | null): Record<string, string> {
  const out: Record<string, string> = {}
  const fromDb = settings?.promptOverrides
  if (isRecord(fromDb)) {
    // DB 里有这个键就信它（哪怕是空对象 = 用户清空过覆盖），不再拿 localStorage 兜底，
    // 否则会把用户已经删掉的旧提示词又"复活"回来。
    for (const [name, body] of Object.entries(fromDb)) {
      if (name && typeof body === 'string' && body.trim()) out[name] = body
    }
    return out
  }
  // 只有在 DB 快照里**根本没有** promptOverrides 时，才用 localStorage 兜底
  // （某些版本的 settings 快照没写全，但每一次改动都会同步 localStorage）
  const raw = readLocalStorage(LEGACY_PROMPT_OVERRIDES_LS_KEY)
  if (!raw) return out
  try {
    const parsed: unknown = JSON.parse(raw)
    if (isRecord(parsed)) {
      for (const [name, body] of Object.entries(parsed)) {
        if (name && typeof body === 'string' && body.trim()) out[name] = body
      }
    }
  } catch {
    // 坏 JSON 当没有：提示词丢了不影响聊天记录
  }
  return out
}

/**
 * 读当前浏览器里所有"老数据"
 *
 * 返回 null = 真的什么都没有（首次访问新站）。读取失败也返回 null，并把原因
 * 打到控制台 —— 界面只负责"没有可迁的"这一种表现，不因读库异常弹错误。
 */
export async function readLegacyLocal(): Promise<LegacyLocalData | null> {
  // 打包端优先（EXE/APK 的真相在文件里）；网页端 native 为 null。
  // 网页端会把当前 origin 下所有"像本应用"的库都读一遍（见 listLegacyDbNames），
  // 正常只有一个；万一有多个（改名/测试库），合并逻辑按卡片身份去重，不会重复。
  const native = await readNativeSnapshot()
  const snapshots = native ? [native] : await readAllDbSnapshots()
  if (snapshots.length === 0) return null

  const sources: string[] = []
  if (native) sources.push(LEGACY_NATIVE_FILE)
  let cards: LegacyCard[] = []
  let settings: Record<string, unknown> | null = null
  let gender: 'male' | 'female' | null = null

  for (const snapshot of snapshots) {
    if (snapshot.shardCards) sources.push(LEGACY_CARD_INDEX_KEY)
    if (snapshot.legacyCards) sources.push(LEGACY_CARDS_KEY)
    if (!settings && snapshot.settings) settings = snapshot.settings
    if (!gender && snapshot.myGender) gender = snapshot.myGender
    // 同一个库里先把分片与旧快照合并，再把多个库的结果按卡片身份并起来
    const merged = mergeLegacyCards(snapshot.shardCards, snapshot.legacyCards)
    cards = mergeLegacyCards(cards, merged)
  }

  if (cards.length === 0 && sources.length === 0) return null

  return {
    cards,
    myGender: gender ?? 'male',
    promptOverrides: readPromptOverrides(settings),
    sources: [...new Set(sources)],
  }
}

// ---------------------------------------------------------------------------
// 对外的三个能力：检测 / 打包 / 迁移
// ---------------------------------------------------------------------------

/**
 * 检测旧数据（**只读**，不改不删）
 *
 * 计数口径与真正迁移时一致：屏上消息优先；屏上一条都没有、但 AI 记忆还在的
 * 段落，会把 AI 记忆算成"兜底进消息"的条数（后端就是这么兜的）。
 */
export async function detectLegacyLocal(): Promise<
  LegacyDetectSummary & { local: LegacyLocalData | null }
> {
  const local = await readLegacyLocal()
  const counts = countLegacyCards(local?.cards ?? [])
  const marker = readMigrateMarker(markerStorage())
  return {
    ...counts,
    hasData: counts.messages > 0,
    promptOverrides: Object.keys(local?.promptOverrides ?? {}).length,
    myGender: local?.myGender ?? 'male',
    migrated: !!marker,
    local,
  }
}

/** 本地数据 → 导入包 JSON 文本（备份文件与上传体是同一份内容） */
export function toImportPackageJson(local: LegacyLocalData): string {
  return JSON.stringify(buildImportPackage(local).package)
}

/** 本地数据 → 导入包 Blob（`POST /v1/imports` 的 file 字段直接用它） */
export function toImportPackage(local: LegacyLocalData): Blob {
  return new Blob([toImportPackageJson(local)], { type: 'application/json' })
}

/** 是否已经迁移过（界面据此不再提示） */
export function isLegacyMigrated(): boolean {
  return !!readMigrateMarker(markerStorage())
}

/** 读迁移标记（给界面显示"上次迁移于…"用） */
export function readLegacyMigrateMarker() {
  return readMigrateMarker(markerStorage())
}

export interface MigrateLegacyOptions {
  /** 进度回调（界面画条） */
  onProgress?: (progress: MigrationProgress) => void
  /** 忽略"已迁移"标记，强制再传一次 */
  force?: boolean
}

/**
 * 把老本地数据迁移到当前设备/账号名下
 *
 * 归属规则（web/back docs/03 §2）：
 *   · 已登录 → `conversations.user_id = 账号 id`，换设备也看得见；
 *   · 访客   → 只挂在这台设备（`device_id`，`user_id` 为空）；之后在这台设备
 *              登录账号时，后端会把它们一并"收编"（`adopted_conversations`），
 *              所以不会出现"导到临时设备后登录就看不见"。
 *
 * 成功后清一次单聊绑定表：新导进来的会话才是带历史的，旧绑定可能指向空会话
 * （与「领完取件码」同一个处理，见 utils/backend.ts forgetAllBindings）。
 */
export async function migrateLegacyLocal(
  options: MigrateLegacyOptions = {},
): Promise<MigrationOutcome> {
  const local = await readLegacyLocal()
  const deps = {
    readLocal: async () => local,
    ensureToken: () => ensureDeviceToken(),
    importUrl: v1Url('/imports'),
    storage: markerStorage(),
    force: options.force,
    onProgress: options.onProgress,
  }

  let outcome: MigrationOutcome
  try {
    outcome = await runMigration(deps)
  } catch (err) {
    // 设备 token 失效：丢旧凭证、重新注册一次再重试（apiClient 的自愈逻辑在这里
    // 用不上，因为上传走的是 XHR，拿不到它的 401 重放）
    if (isAuthError(err)) {
      devWarn('[legacy] 设备凭证失效，重新注册后重试一次')
      forgetDeviceToken()
      outcome = await runMigration({ ...deps, force: options.force })
    } else {
      throw err
    }
  }

  if (!outcome.skipped) {
    // 绑定表清空：下一次发消息会重新按"服务端最近/消息最多的会话"绑定，
    // 否则可能还指着迁移前那个空会话（界面有记录、AI 却完全不记得）
    try {
      forgetAllBindings()
    } catch (err) {
      devWarn('[legacy] 清理单聊绑定失败（不影响迁移结果）', err)
    }
    devInfo(
      '[legacy] 迁移完成',
      outcome.batchIds.join(','),
      `${outcome.counts.conversations} 会话 / ${outcome.counts.messages} 消息`,
    )
  }
  return outcome
}

function isAuthError(err: unknown): boolean {
  return isRecord(err) && err.kind === 'auth'
}

// ---------------------------------------------------------------------------
// 手动导入：用户在「从旧版导入」里自己选一份旧客户端存档
// ---------------------------------------------------------------------------

/**
 * 从用户选择的旧客户端存档导入
 *
 * 为什么不走 `readLegacyLocal()`：那个函数只认"当前应用自己的数据目录"（新装
 * 的 Baker AI 4 目录是空的）。旧客户端可能装在别处、也可能是别人拷来的文件，
 * 所以由用户自己选文件，这里只负责解析 + 走同一套上传管线。
 *
 * **只读文件**：不移动、不删除、不改写用户选中的那份存档。
 *
 * @throws 文件不是 JSON / 不是本应用的存档 / 没有可导入的聊天记录（界面直接显示 message）
 */
export async function migrateLegacyFromFile(
  file: File,
  options: MigrateLegacyOptions = {},
): Promise<MigrationOutcome> {
  let parsed: unknown
  try {
    const text = await file.text()
    parsed = JSON.parse(text)
  } catch {
    throw new Error('这个文件不是有效的 JSON，请选择旧客户端的 endfield-baker-data.json')
  }

  const local = parseLegacyNativeJson(parsed)
  if (!local) {
    throw new Error('文件格式不对：没有找到旧版的数据结构（cards）')
  }
  const counts = countLegacyCards(local.cards)
  if (counts.messages === 0) {
    throw new Error('这个存档里没有可导入的聊天记录（可能是空档，或只有占位卡）')
  }

  // 手动导入不认"已迁移"标记：用户可能想把另一台设备的存档也并进来
  const deps = {
    readLocal: async () => local,
    ensureToken: () => ensureDeviceToken(),
    importUrl: v1Url('/imports'),
    storage: markerStorage(),
    force: true,
    onProgress: options.onProgress,
  }

  let outcome: MigrationOutcome
  try {
    outcome = await runMigration(deps)
  } catch (err) {
    if (isAuthError(err)) {
      devWarn('[legacy] 手动导入时设备凭证失效，重新注册后重试一次')
      forgetDeviceToken()
      outcome = await runMigration({ ...deps, force: true })
    } else {
      throw err
    }
  }

  if (!outcome.skipped) {
    try {
      forgetAllBindings()
    } catch (err) {
      devWarn('[legacy] 手动导入后清理单聊绑定失败（不影响导入结果）', err)
    }
    devInfo(
      '[legacy] 手动导入完成',
      outcome.batchIds.join(','),
      `${outcome.counts.conversations} 会话 / ${outcome.counts.messages} 消息`,
    )
  }
  return outcome
}

// ---------------------------------------------------------------------------
// 「先备份一份」：把老数据原样导出成文件（格式与导入包一致，将来能直接导回来）
// ---------------------------------------------------------------------------

export interface LegacyBackupResult {
  filename: string
  bytes: number
  /** 含多少张卡 / 多少段会话 / 多少条消息（与迁移口径一致） */
  counts: { cards: number; conversations: number; messages: number }
}

/**
 * 下载一份本地老数据的备份
 *
 * 目的不是"给新后端用"（迁移会自己打包），而是**在覆盖部署前给用户留个底**：
 * 浏览器数据有可能被清理、被无痕模式清空、被别的标签页误删。文件在用户手里
 * 才是真的安全。格式与导入包相同，以后想恢复也能直接导入。
 */
export async function downloadLegacyBackup(): Promise<LegacyBackupResult> {
  const local = await readLegacyLocal()
  if (!local) throw new Error('没有找到旧版本地数据')
  const json = toImportPackageJson(local)
  const blob = new Blob([json], { type: 'application/json' })
  const counts = countLegacyCards(local.cards)
  const stamp = fileStamp()
  const filename = `BAKER-legacy-backup-${stamp}.json`

  // 动态引入：导出层会拖进 JSZip，只有点「备份」的人需要为它付体积
  const { downloadBlob } = await import('./zipExport')
  const bytes = await downloadBlob(blob, filename)
  return {
    filename,
    bytes,
    counts: {
      cards: counts.cards,
      conversations: counts.conversations,
      messages: counts.messages,
    },
  }
}

function fileStamp(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  )
}

// ---------------------------------------------------------------------------
// 覆盖部署的善后：老 Service Worker / 缓存
// ---------------------------------------------------------------------------

/**
 * 清掉老站留下的 Service Worker 与缓存（同一台浏览器只清一次）
 *
 * 为什么需要：老网页源码里**没有**注册 SW（`v322kit/src` 全库搜不到
 * `serviceWorker` / `registerSW` / `workbox`），但线上曾经部署过的版本、或者
 * 用户从别处打开的页面可能装过。SW 一旦接管，覆盖部署后它还会继续用缓存里的
 * 旧 `index.html` 喂给用户 —— 表现就是"更新了但界面没变、数据也读不到"。
 *
 * 只清一次：标记写进 `bakerai.legacy-sw-cleaned.v1`。将来新站如果自己上 SW，
 * 不会被这段逻辑反复清掉。
 */
export async function cleanupLegacyServiceWorkersOnce(): Promise<boolean> {
  const storage = markerStorage()
  if (storage) {
    try {
      if (storage.getItem(SW_CLEAN_MARKER_KEY) === '1') return false
    } catch {
      // 读不到标记就当成没清过（清理是幂等的）
    }
  }

  let changed = false
  try {
    if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations()
      for (const reg of regs) {
        const ok = await reg.unregister()
        changed = changed || ok
      }
    }
  } catch (err) {
    devWarn('[legacy] 注销老 Service Worker 失败', err)
  }

  try {
    if (typeof caches !== 'undefined') {
      const names = await caches.keys()
      for (const name of names) {
        const ok = await caches.delete(name)
        changed = changed || ok
      }
    }
  } catch (err) {
    devWarn('[legacy] 清理 Cache Storage 失败', err)
  }

  if (storage) {
    try {
      storage.setItem(SW_CLEAN_MARKER_KEY, '1')
    } catch {
      // 写不进标记：下次启动会再清一遍，清理本身无害
    }
  }
  if (changed) devInfo('[legacy] 已清理老 Service Worker / 缓存')
  return changed
}
