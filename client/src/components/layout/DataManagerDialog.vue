<script setup lang="ts">
// =============================================================================
// 数据管理弹窗(DataManagerDialog)
// -----------------------------------------------------------------------------
// 提供全局操作(控制全部对话):
//   - 数据统计(卡片 / 对话 / 消息 / 序列化大小)
//   - 导出工程(两种格式,直接下载):
//       · 导出数据 = .zip 压缩包(文本 JSON + 独立图片文件)
//       · 导出 JSON = 单文件 JSON(旧版兼容格式,图片 dataURL 内联)
//   - 导入工程(选文件 → 解析校验 → 二次确认 → 整体替换):
//       · .zip 走压缩包解压(见 zipExport)
//       · .json 走旧版兼容层(见 jsonCompat:自动识别旧格式并补全缺失字段)
//   - 清空全部对话(删除所有子对话,每个角色保留一个空子对话,清除消息与上下文)
//   - 清空全部消息(仅清空可见消息,AI 上下文记忆保留)
//   - 清空全部上下文(仅清空 AI 记忆,可见消息保留)
//   - 一键清空历史与上下文(同时清空全部消息 + 全部上下文)
//     ⚠ 2026-11-18 起这四条**同时清服务器**（POST /v1/data/clear，见 utils/dataClear.ts）：
//       用户实测"这几个好像不管服务器的数据啊"—— 以前只清本机，换台设备一登录
//       会话和记忆全回来了。现在顺序是**先服务器、后本机**，服务器失败就一个字都不动。
//   - 云同步（取件码中转，2026-11-18 起接到我们自己的新后端）：
//       · 「一键上传数据」= 把本地导出的同一份包上传，换一个 8 位取件码（24 小时有效）
//       · 「拉取历史数据」= 输入取件码 → 先验码（免登录）→ 再领取（要登录）
//         领取 = 后端把这批会话直接落到当前账号/设备名下（可多台设备重复领取）
//       · 未登录时不让人输码：直接指去「设置 → 账号」注册（注册时填码最省事）
//
// 破坏性操作均为两段式确认(弹窗内切换确认态),不动用现有素材图。
// =============================================================================
import { computed, ref, watch, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useChatStore } from '../../stores/chat'
import { useSettingsStore } from '../../stores/settings'
import VersionDialogShell from './VersionDialogShell.vue'
import { MATERIALS } from '../../constants/materials'
import {
  downloadProject,
  exportToZipBytes,
  importFromZip,
  getNativeSaveFile,
  projectExportFileName,
  type ProjectPayload,
} from '../../utils/zipExport'
import {
  downloadProjectJson,
  importFromJson,
  exportToJson,
  IMPORT_FILE_ACCEPT,
} from '../../utils/jsonCompat'
import {
  isNativeAndroid,
  saveExportBytesSmart,
  withTimeout,
  type ExportProgress,
} from '../../utils/nativeExport'
import { isElectron, isNative } from '../../utils/platform'
import {
  uploadCloudData,
  verifyUploadCode,
  claimUploadCode,
  describeClaimResult,
  formatBytes,
  formatSpeed,
  formatDuration,
  formatExpiresAt,
  isClaimCodeValid,
  normalizeClaimCode,
  type CloudUploadResult,
  type CloudClaimResult,
  type CloudProgress,
} from '../../utils/cloudData'
import { forgetAllBindings } from '../../utils/backend'
// 服务器端清理（2026-11-18）：四个危险按钮以前只清本机，服务器上的数据一条没动
import { hasDeviceToken } from '../../utils/apiClient'
import { clearServerData, describeServerClear, type DataClearResult } from '../../utils/dataClear'
import { useAccountStore } from '../../stores/account'
import { useListStore } from '../../stores/list'
import {
  importFromZipInWorker,
  terminateZipWorker,
} from '../../utils/zipWorkerClient'
import { flushPendingWrites } from '../../composables/useChatPersistence'
import { mergeCards, describeMerge } from '../../utils/importMerge'
import { migrateLegacyFromFile, describeMigration } from '../../utils/legacyLocalData'
import type { Card } from '../../types/chat'

const props = defineProps<{
  /** 是否展开(由 App 的清除数据按钮控制) */
  open: boolean
  /**
   * 内嵌模式(设置弹窗内嵌时置 true):
   * 不渲染遮罩/关闭按钮/标题,面板铺满所在容器宽度,仅保留功能内容。
   */
  embedded?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /**
   * 让父组件把设置面板切到「账号」页
   *
   * 为什么由这里发事件而不是直接改父组件的 tab：数据管理是**内嵌**在设置里的
   * 子组件，它不该知道设置面板内部有哪几页；只表达"用户想去账号页"这个意图。
   */
  (e: 'goto-account'): void
}>()

const chatStore = useChatStore()
const settingsStore = useSettingsStore()
const accountStore = useAccountStore()
// 列表数据源（P1）：领取成功后刷新列表，导入的会话立刻可见
const listStore = useListStore()
const { cards } = storeToRefs(chatStore)

// 组件卸载时终止导入导出 Worker,释放内存(大文件场景尤为关键)
onUnmounted(() => {
  terminateZipWorker()
})

/** 数据统计(卡片 / 对话 / 消息 / 序列化大小) */
const stats = computed(() => {
  // 对话只统计有内容的(有消息或有 AI 记忆),空对话不计入
  const hasContent = (cv: { messages: unknown[]; contextHistory?: unknown[] }) =>
    cv.messages.length > 0 || (cv.contextHistory?.length ?? 0) > 0
  const convCount = cards.value.reduce(
    (n, c) => n + c.conversations.filter(hasContent).length,
    0,
  )
  const msgCount = cards.value.reduce(
    (n, c) => n + c.conversations.reduce((m, cv) => m + cv.messages.length, 0),
    0,
  )
  const sizeKB = JSON.stringify({
    cards: cards.value,
  }).length / 1024
  return { cardCount: cards.value.length, convCount, msgCount, sizeKB: sizeKB.toFixed(1) }
})

// ---- 导出状态（恢复为正式版实现）-------------------------------------------
/** 导出中（ZIP 正在序列化+压缩） */
const isExporting = ref(false)
/** JSON 导出中 */
const isExportingJson = ref(false)
/** 应急导出中（仅安卓） */
const isExportingEmergency = ref(false)
/** 导出结果提示（成功后的体积/过滤信息） */
const exportInfo = ref('')

/**
 * 导出进度（2026-11-22）
 *
 * 为什么要在界面上给：24.8MB / 1.2 万条消息的存档，安卓上「打包 → 写盘 → 校验」
 * 要走几十秒，以前界面上只有「导出中…」三个字，用户的反馈就是"点了没反应、
 * 按钮一直灰"。现在显示到「哪一步 / 百分比 / 已经多少秒」，
 * 卡住时至少能看出卡在哪一步。
 */
const exportKind = ref<'' | 'zip' | 'json'>('')
const exportStage = ref<ExportProgress['stage'] | ''>('')
const exportPercent = ref(-1)
const exportElapsed = ref(0)
const exportStartedAt = ref(0)
let exportTicker: ReturnType<typeof setInterval> | null = null

/** 打包阶段的硬上限：纯 JS 序列化+压缩，超过这个时间就是真卡住了 */
const PACK_TIMEOUT_MS = 240_000

/** 开始一次导出：置进度状态 + 启动秒表（每 250ms 刷一次已用秒数） */
function beginExportState(kind: 'zip' | 'json') {
  exportKind.value = kind
  exportStage.value = 'pack'
  exportPercent.value = -1
  exportElapsed.value = 0
  exportStartedAt.value = Date.now()
  if (exportTicker) clearInterval(exportTicker)
  exportTicker = setInterval(() => {
    exportElapsed.value = Math.floor((Date.now() - exportStartedAt.value) / 1000)
  }, 250)
}

/** 结束导出：停表 + 清状态（成功失败都要走，绝不让按钮永远灰着） */
function endExportState() {
  if (exportTicker) clearInterval(exportTicker)
  exportTicker = null
  exportKind.value = ''
  exportStage.value = ''
  exportPercent.value = -1
  exportElapsed.value = 0
}

/** 原生写入的进度回调（打包阶段由调用方自己置 pack） */
function onExportProgress(p: ExportProgress) {
  if (!exportKind.value) return
  if (p.stage) exportStage.value = p.stage
  exportPercent.value = p.percent
}

/** 进度文案：「正在打包… 12s」/「正在写入 42% · 30s」 */
const exportStatusText = computed(() => {
  const secs = exportElapsed.value
  const stage = exportStage.value
  const pct = exportPercent.value
  const what = stage === 'append'
    ? `正在写入${pct >= 0 ? ' ' + pct + '%' : ''}`
    : stage === 'finish'
      ? '正在校验文件'
      : stage === 'begin'
        ? '正在建立文件'
        : stage === 'encode'
          ? '正在编码'
          : stage === 'getplugin'
            ? '正在连接保存组件'
            : '正在打包数据'
  const size = stats.value.sizeKB
  return `${what}… ${secs}s（数据 ${size} KB）`
})

/** 按钮上的短文案 */
const exportButtonLabel = computed(() => {
  if (!isExporting.value) return '导出数据'
  const pct = exportPercent.value
  return pct >= 0 && exportStage.value === 'append' ? `写入 ${pct}%` : '导出中…'
})

const exportJsonButtonLabel = computed(() => {
  if (!isExportingJson.value) return '导出 JSON'
  return `导出中… ${exportElapsed.value}s`
})

/** 是否安卓端（决定是否显示"应急导出"按钮） */
const androidNative = isNativeAndroid()

/** 是否打包环境（Electron/APK）——正式版同款判定 */
function isNativePlatform(): boolean {
  // 统一走 utils/platform：Electron / Android / iOS 都算"打包端"，
  // 走 exportToZipInWorker（打包+base64）+ 各自的原生保存通道；
  // 纯浏览器（含手机浏览器）走 downloadProject 的 <a download>。
  return isElectron() || isNative()
}

/**
 * 导出后提示（大图处理结果 + 真实体积）
 *
 * 两类处理分开报：重编码是"图还在，只是压小了"，剔除是"图没进包，只剩占位"。
 * 以前只报一个 filteredImages，用户看到"已过滤 N 张"会以为图被弄丢了（B7）。
 *
 * totalBytes 由 utils 返回**真实字节**（ZIP = 落盘/blob.size，JSON = UTF-8 长度），
 * 不再是估算值 —— 提示里的 MB 数必须和用户拿到的文件对得上（B12）。
 */
function applyExportInfo(
  filteredImages: number,
  reencodedImages: number,
  totalBytes: number,
  kind: string,
) {
  const mb = (totalBytes / 1024 / 1024).toFixed(1)
  const notes: string[] = []
  if (reencodedImages > 0) {
    notes.push(
      `已重编码 ${reencodedImages} 张大图（最长边 1600、画质略有压缩，体积已显著减小）`,
    )
  }
  if (filteredImages > 0) {
    notes.push(`已剔除 ${filteredImages} 张超大图片（保留"[图片]"占位）`)
  }
  exportInfo.value = `${kind}完成(约 ${mb}MB)。` + (notes.length > 0 ? notes.join('；') + '。' : '')
}

// ---- 两段式确认状态 ---------------------------------------------------------
type ConfirmKind = 'clear' | 'clearMessages' | 'clearContext' | 'clearEverything' | 'import' | null
const confirmKind = ref<ConfirmKind>(null)
const importError = ref('')
const pendingImport = ref<ProjectPayload | null>(null)

// ---- 危险操作：服务器那一半的状态（2026-11-18） -------------------------------
/** 这次确认是否要连服务器一起清（没 token / 没用服务器数据时只清本机） */
const serverClearEnabled = ref(false)
/** 清理请求进行中（按钮置灰、文案变"清理中…"，防止连点两次把服务器清两遍） */
const clearing = ref(false)
/** 清理失败的原因（明确写"服务器没清掉"，不假装成功） */
const clearError = ref('')
/** 清理成功后的结果（本机 + 服务器计数）；非 null 时面板里显示一段绿字 */
const clearResult = ref<{ title: string; local: boolean; server: DataClearResult | null } | null>(
  null,
)

/** 确认弹窗里"会同时删除服务器上的数据"那一句（只在真的会清服务器时出现） */
const confirmServerNote = computed(() =>
  serverClearEnabled.value
    ? '会同时删除服务器上的数据（会话、消息、她的记忆），无法恢复。'
    : '',
)

const confirmTexts: Record<Exclude<ConfirmKind, null>, string> = {
  clear: '将删除全部对话，确定吗？',
  clearMessages: '将清空全部对话的消息（上下文记忆保留），确定吗？',
  clearContext: '将清空全部对话的上下文（消息记录保留），确定吗？',
  clearEverything: '将同时清空全部消息与全部上下文（AI 将不再记得之前的对话），确定吗？',
  import: '导入将覆盖当前全部数据，确定吗？',
}

/** 确认弹窗的标题(与触发按钮同名,让玩家一眼看出自己在确认什么) */
const confirmTitles: Record<Exclude<ConfirmKind, null>, string> = {
  clear: '删除全部对话',
  clearMessages: '清空全部消息',
  clearContext: '清空全部上下文',
  clearEverything: '一键清空历史与上下文',
  import: '导入数据',
}

/** 当前确认弹窗标题(null 时给个兜底,弹窗本身不显示) */
const confirmTitle = computed(() =>
  confirmKind.value ? confirmTitles[confirmKind.value] : '确认操作',
)

const fileInput = ref<HTMLInputElement | null>(null)

// ---- 从旧版导入（Baker AI 4 新增）-----------------------------------------
// 新客户端的数据目录和旧版完全分开（这是"不覆盖安装"的前提），所以旧存档
// 不会自动出现在本机。这里让用户自己选旧客户端的 endfield-baker-data.json：
//   · 只读文件，不移动/不删除/不改写
//   · 解析后走和自动迁移同一套上传管线（挂当前账号/设备）
//   · 手动导入不认"已迁移"标记，用户可以把另一台设备的存档也并进来
const legacyFileInput = ref<HTMLInputElement | null>(null)
const legacyImporting = ref(false)
const legacyNote = ref('')
const legacyError = ref('')
const legacyPercent = ref(-1)

function onPickLegacyFile() {
  if (legacyImporting.value) return
  legacyError.value = ''
  legacyNote.value = ''
  legacyPercent.value = -1
  legacyFileInput.value?.click()
}

async function onLegacyFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || legacyImporting.value) return

  legacyImporting.value = true
  legacyError.value = ''
  legacyNote.value = '正在读取旧存档…'
  legacyPercent.value = -1
  try {
    const outcome = await migrateLegacyFromFile(file, {
      onProgress: (p) => {
        legacyPercent.value = typeof p.percent === 'number' ? p.percent : -1
        const piece = p.piece && p.pieces && p.pieces > 1 ? `（第 ${p.piece}/${p.pieces} 批）` : ''
        if (p.phase === 'reading') legacyNote.value = '正在读取旧存档…'
        else if (p.phase === 'uploading') legacyNote.value = `正在上传${piece}…`
        else legacyNote.value = `服务器正在写入${piece}…`
      },
    })
    legacyNote.value = outcome.skipped ? '这份存档之前导入过，已跳过。' : describeMigration(outcome)
    // 导入的会话在服务端，列表要刷新一次才能立刻看到
    await listStore.afterClaim()
  } catch (err) {
    legacyError.value = err instanceof Error ? err.message : '旧版存档导入失败'
    legacyNote.value = ''
  } finally {
    legacyImporting.value = false
    legacyPercent.value = -1
  }
}

/** 导入合并进行中（合并是同步的，但数据量大时仍会让出一次绘制） */
const isMergingImport = ref(false)

/**
 * 导出数据(.zip)
 *
 * 两条路径，简单可靠：
 *   1. 打包端(EXE/APK)：exportToZipBytes(打包直接出字节) → saveExportBytesSmart()
 *      —— 小文件走系统保存框；大文件走分块写入 + 收尾回读校验，失败明确报错。
 *      2026-11-22：不再整包转 base64 再解码回来（少搬两趟几十 MB 内存）。
 *   2. 浏览器：downloadProject() 直接下载
 *
 * 全程有进度（阶段 + 已用秒数），打包段还有硬上限，不会出现"按钮永远灰着"。
 */
async function onExport() {
  if (isExporting.value || isExportingJson.value) return
  isExporting.value = true
  beginExportState('zip')
  exportInfo.value = ''
  importError.value = ''
  try {
    // 空数据守卫(2026-10-04)：以前这种情况会"成功"导出一个 0 字节空包，
    // 用户拿到手以为数据丢了 —— 直接说清楚更有用
    if (!cards.value || cards.value.length === 0) {
      throw new Error('当前没有可导出的数据（列表为空）。若你确认有聊天记录，请先重启应用再试；' +
                      '仍为空说明本地数据文件异常，可把数据目录里的 endfield-baker-data.json 发我排查。')
    }
    // 文件名统一用 zipExport 的本地时间戳：以前这里用 toISOString()（UTC），
    // 同一时刻导出的 ZIP 与 JSON 会差 8 小时，用户按时间对不上（B11）。
    const fileName = projectExportFileName()
    if (isNativePlatform()) {
      // 2026-11-22：打包直接出字节，再交给字节版保存通道 ——
      // 旧链路是「打包 → 整包 base64(35MB) → 又解码回字节 → 分块」，
      // 24.8MB 的存档在低端机上白白多搬两趟内存，界面上还看不到任何进度。
      //
      // 打包段也加硬上限：这一段是纯 JS，以前没有任何超时，真卡住时
      // 按钮就永远灰着、连报错都没有。
      const packed = await withTimeout(
        exportToZipBytes(
          cards.value,
          chatStore.myGender,
          chatStore.stripVariantIndex,
          settingsStore.promptOverrides,
          settingsStore.getSettingsSnapshot(),
        ),
        PACK_TIMEOUT_MS,
        '打包导出文件',
      )
      exportStage.value = 'begin'
      exportPercent.value = -1
      // 保存通道：小文件走系统保存框（带空内容守卫），大文件走分块写入
      // （逐块 SHA-256 + 收尾回读校验），失败会明确报错。
      const saved = await saveExportBytesSmart({
        fileName,
        bytes: packed.bytes,
        mime: 'application/zip',
        onProgress: onExportProgress,
      })
      // 优先用落盘字节（分块通道是回读校验过的），拿不到再退回 blob 真实体积（B12）
      applyExportInfo(
        packed.filteredImages, packed.reencodedImages,
        saved.bytes || packed.totalBytes, '导出数据',
      )
    } else {
      const { filteredImages, reencodedImages, totalBytes } = await downloadProject(
        cards.value,
        chatStore.myGender,
        chatStore.stripVariantIndex,
        settingsStore.promptOverrides,
        settingsStore.getSettingsSnapshot(),
      )
      applyExportInfo(filteredImages, reencodedImages, totalBytes, '导出数据')
    }
  } catch (err) {
    importError.value = err instanceof Error ? err.message : '导出失败'
  } finally {
    isExporting.value = false
    endExportState()
  }
}

/** 导出为单文件 JSON（正式版同款：downloadProjectJson 内部处理平台差异） */
async function onExportJson() {
  if (isExportingJson.value || isExporting.value) return
  isExportingJson.value = true
  beginExportState('json')
  exportInfo.value = ''
  importError.value = ''
  try {
    const { filteredImages, reencodedImages, totalBytes } = await downloadProjectJson(
      cards.value,
      chatStore.myGender,
      chatStore.stripVariantIndex,
      settingsStore.promptOverrides,
      settingsStore.getSettingsSnapshot(),
      // v7：把当前列表人物顺序一起写进包（覆盖导入时应用）
      chatStore.exportCardOrder(),
      // 安卓：边序列化边分块写盘，进度回到底部那行（不再是一个含糊的「导出中…」）
      onExportProgress,
    )
    applyExportInfo(filteredImages, reencodedImages, totalBytes, '导出 JSON')
  } catch (err) {
    importError.value = err instanceof Error ? err.message : '导出失败'
  } finally {
    isExportingJson.value = false
    endExportState()
  }
}

/** 应急导出（仅安卓）——沿用正式版的 JSON 导出通道 */
async function onEmergencyExport() {
  if (isExportingEmergency.value) return
  isExportingEmergency.value = true
  exportInfo.value = ''
  importError.value = ''
  try {
    const { filteredImages, reencodedImages, totalBytes } = await downloadProjectJson(
      cards.value,
      chatStore.myGender,
      chatStore.stripVariantIndex,
      settingsStore.promptOverrides,
      settingsStore.getSettingsSnapshot(),
      // v7：把当前列表人物顺序一起写进包（覆盖导入时应用）
      chatStore.exportCardOrder(),
    )
    applyExportInfo(filteredImages, reencodedImages, totalBytes, '应急导出')
  } catch (err) {
    importError.value = err instanceof Error ? err.message : '应急导出失败'
  } finally {
    isExportingEmergency.value = false
  }
}


// ---- 云同步（取件码中转） ----------------------------------------------------
/** 上传中 */
const cloudBusy = ref(false)
/** 验码 / 领取中 */
const cloudFetching = ref(false)
/** 上传成功后拿到的取件码信息 */
const cloudResult = ref<CloudUploadResult | null>(null)
/** 领取成功后拿到的导入摘要（几段会话 / 几条消息 / 几张图） */
const claimResult = ref<CloudClaimResult | null>(null)
/** 领取用的取件码输入 */
const cloudCodeInput = ref('')

/**
 * 取件码输入上限 = 8 + 2
 *
 * 取件码恰好 8 位，但用户从别处抄过来常常多打/多粘一两个字符。
 * 留 2 个字符的容错：既不会因为多一位就完全无法输入，也不会让人往框里粘一整篇文章。
 * 提交（或失焦）时会用 normalizeClaimCode 净化成 8 位。
 */
const CODE_LEN = 8
const CODE_MAX_INPUT = CODE_LEN + 2

/**
 * 输入处理
 *
 * ⚠️ 这里**故意不静默过滤非法字符** —— 否则用户打了符号输入框永远不会变红，
 * 与"输入其他符号直接变红"的预期不符。做法：
 *   · 统一转大写、去掉空白（空格不算"非法符号"，粘贴带空格很常见）
 *   · 只在超过上限时截断
 *   · 非法字符**留在输入框里**，由 codeInvalid 把输入框变红、按钮置灰
 */
function onCodeInput(e: Event) {
  const el = e.target as HTMLInputElement
  const cleaned = (el.value || '').toUpperCase().replace(/\s+/g, '').slice(0, CODE_MAX_INPUT)
  if (cleaned !== el.value) el.value = cleaned
  cloudCodeInput.value = cleaned
}

/** 输入是否可提交：清洗后恰好 8 位字母数字（见 cloudData.isClaimCodeValid） */
const codeValid = computed(() => isClaimCodeValid(cloudCodeInput.value))

/** 是否显示了非法字符（空输入不算无效，避免一打开就报错） */
const codeInvalid = computed(
  () => cloudCodeInput.value.length > 0 && /[^A-Z0-9]/.test(cloudCodeInput.value),
)

/** 失焦时净化掉非法字符 / 多余字符（用户已经看到红字提示了，这里帮他改好） */
function onCodeBlur() {
  const clean = sanitizeCode(cloudCodeInput.value)
  if (clean !== cloudCodeInput.value) cloudCodeInput.value = clean
}

/** 只保留 8 位大写字母数字（失焦 / 提交时用） */
function sanitizeCode(raw: string): string {
  return normalizeClaimCode(raw)
}

/** 云同步区提示 / 错误 */
const cloudInfo = ref('')
const cloudError = ref('')
/** 是否展开"输入取件码领取"区域 */
const cloudFetchOpen = ref(false)

/** 当前是否已登录（领取要求登录：数据要落到账号名下） */
const isLoggedIn = computed(() => accountStore.state === 'user')

/** 领取摘要（界面直接显示这一句 + 明细） */
const claimSummary = computed(() =>
  claimResult.value ? describeClaimResult(claimResult.value) : '',
)

/**
 * 云同步进度：阶段 + 最近一帧传输数据(字节/速度/耗时/预计剩余)。
 * 阶段说明：packing=本地打包导出；upload=上传；verify=验码；claim=领取并导入。
 */
type CloudStage = 'idle' | 'packing' | 'upload' | 'verify' | 'claim'
const cloudStage = ref<CloudStage>('idle')
const cloudProgress = ref<CloudProgress | null>(null)

/** 让浏览器先画一帧再跑重活(打包会阻塞主线程，否则进度条不出现) */
const paint = () => new Promise<void>((r) => setTimeout(r, 32))

/** 进度条宽度(%)：总量未知时返回 -1(走不确定动画) */
const cloudPercent = computed(() => {
  const p = cloudProgress.value
  if (cloudStage.value !== 'upload') return -1
  // 字节传完但服务器还在处理 → 用不确定动画，避免"卡在 100%"的观感
  if (p?.waitingServer) return -1
  if (!p || p.percent < 0) return -1
  return p.percent
})

/** 进度条右侧文字 */
const cloudProgressText = computed(() => {
  if (cloudStage.value === 'packing') return '正在打包数据…'
  if (cloudStage.value === 'verify') return '正在校验取件码…（这一步不需要登录）'
  if (cloudStage.value === 'claim') return '取件码有效，正在领取并导入到你的账号…'
  const p = cloudProgress.value
  if (!p) return '正在上传…'
  if (p.waitingServer) {
    // 数据已发完，服务器在解包/写库 —— 明确告知，别让用户以为卡死
    return `已上传 ${formatBytes(p.loaded)}，正在等待服务器处理…（已用 ${formatDuration(p.elapsedMs)}）`
  }
  const size = p.total > 0 ? `${formatBytes(p.loaded)} / ${formatBytes(p.total)}` : formatBytes(p.loaded)
  const parts = [size, formatSpeed(p.speedBps), p.percent >= 0 ? `${p.percent}%` : '传输中']
  parts.push(`已用 ${formatDuration(p.elapsedMs)}`)
  if (p.etaMs !== null) parts.push(`剩余约 ${formatDuration(p.etaMs)}`)
  if (p.total <= 0) parts.push('(服务器未提供总长度)')
  return parts.join(' · ')
})

/** 传输开始时的统一初始化 */
function startCloudProgress(stage: CloudStage) {
  cloudStage.value = stage
  cloudProgress.value = null
}
function endCloudProgress() {
  cloudStage.value = 'idle'
  cloudProgress.value = null
}
/** 传给 cloudData 的回调 */
function onCloudProgress(p: CloudProgress) {
  cloudStage.value = p.phase === 'upload' ? 'upload' : 'claim'
  cloudProgress.value = p
}

/**
 * 一键上传：用与本地「导出 JSON」完全相同的包（exportToJson）上传
 *
 * 为什么复用 exportToJson 而不是另造一份：用户看到的就是"我导出的东西传上去了"，
 * 包里字段（卡/会话/消息/图片/设置）与本地导出逐字节一致，将来导入端不需要第二个解析器。
 */
async function onCloudUpload() {
  if (cloudBusy.value) return
  cloudBusy.value = true
  cloudInfo.value = ''
  cloudError.value = ''
  cloudResult.value = null
  claimResult.value = null
  startCloudProgress('packing')
  await paint()
  try {
    const { json, filteredImages } = exportToJson(
      cards.value,
      chatStore.myGender,
      chatStore.stripVariantIndex,
      settingsStore.promptOverrides,
      settingsStore.getSettingsSnapshot(),
      // v7：把当前列表人物顺序一起写进包（覆盖导入时应用）
      chatStore.exportCardOrder(),
    )
    startCloudProgress('upload')
    const r = await uploadCloudData(json, onCloudProgress)
    cloudResult.value = r
    const mb = (r.bytes / 1024 / 1024).toFixed(1)
    cloudInfo.value =
      `已上传（约 ${mb}MB）。` +
      (filteredImages > 0 ? `已自动过滤 ${filteredImages} 张超大图片。` : '') +
      '把下面的取件码记下来：24 小时内可以在任意设备领取，而且可以重复领取。'
  } catch (err) {
    cloudError.value = err instanceof Error ? err.message : '上传失败'
  } finally {
    endCloudProgress()
    cloudBusy.value = false
  }
}

/** 展开 / 收起"输入取件码领取"（展开时顺手确认一次登录状态） */
function toggleCloudFetch() {
  cloudFetchOpen.value = !cloudFetchOpen.value
  cloudInfo.value = ''
  cloudError.value = ''
  if (cloudFetchOpen.value && accountStore.state === 'unknown' && !accountStore.busy) {
    void accountStore.refresh(true)
  }
}

/**
 * 领取历史数据：先验码（免登录）→ 再领取（要登录）
 *
 * 分两步而不是直接 claim：
 *   · 验码能立刻区分"输错了"（404）和"过期了"（410），提示更准；
 *   · 领取失败时（401 没登录）也不会白跑一次写库。
 */
async function onCloudFetch() {
  if (cloudFetching.value) return
  // 前置校验：不合法直接不发请求（按钮此时本就是灰的，这里再兜一层）
  if (!codeValid.value) return
  cloudFetching.value = true
  cloudInfo.value = ''
  cloudError.value = ''
  claimResult.value = null
  startCloudProgress('verify')
  try {
    const info = await verifyUploadCode(normalizeClaimCode(cloudCodeInput.value))
    cloudStage.value = 'claim'
    const result = await claimUploadCode(info.code)
    claimResult.value = result
    // 领取会在服务端**新建**一条会话装着导入的历史；本地绑定还指着旧会话，
    // 不清掉的话下一句话还是接在旧会话上，等于"领了却看不见"。
    forgetAllBindings()
    // P1 验收核心：领取完立刻让列表可见（必要时自动切到服务端列表）
    void listStore.afterClaim()
    // 设备列表 / 账号状态顺手刷新（失败不影响领取本身）
    void accountStore.refresh(true, true)
    cloudFetchOpen.value = false
  } catch (err) {
    cloudError.value = err instanceof Error ? err.message : '领取失败'
  } finally {
    endCloudProgress()
    cloudFetching.value = false
  }
}

/** 复制取件码 */
async function onCopyCloudCode() {
  const code = cloudResult.value?.code
  if (!code) return
  try {
    await navigator.clipboard.writeText(code)
    cloudInfo.value = '取件码 ' + code + ' 已复制到剪贴板'
  } catch {
    cloudInfo.value = '取件码：' + code + '（选中手动复制）'
  }
}

/** 一键清空:同时清空全部消息 + 全部上下文(AI 不再记得之前的对话) */
function onRequestClearEverything() {
  requestConfirm('clearEverything')
}

// ---- 破坏性操作:统一两段式确认(先切确认态,再由 onConfirm 真正执行) ---------
/**
 * 打开二次确认
 *
 * 除了切确认态，这里还要决定"这次要不要连服务器一起清"：
 *   · 手里有设备凭证（= 真的和服务端打过交道，服务器上可能有这台设备的数据）
 *   · 并且（已登录 或 列表源是 server）—— 也就是"你正在用服务器上的数据"
 * 两条都满足才动服务器。纯本机用户（没 token）文案与行为完全不变。
 */
function requestConfirm(kind: Exclude<ConfirmKind, null | 'import'>) {
  serverClearEnabled.value = hasDeviceToken() && (isLoggedIn.value || listStore.source === 'server')
  // 换一次操作就把上一次的结果/错误清掉，免得绿字挂在新一轮确认下面
  clearResult.value = null
  clearError.value = ''
  confirmKind.value = kind
}

/** 删除全部对话 */
function onRequestClear() {
  requestConfirm('clear')
}

/** 清空全部消息(保留 AI 上下文记忆) */
function onRequestClearMessages() {
  requestConfirm('clearMessages')
}

/** 清空全部上下文(保留可见消息) */
function onRequestClearContext() {
  requestConfirm('clearContext')
}

function onPickFile() {
  importError.value = ''
  fileInput.value?.click()
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    // 按扩展名/MIME 分流:JSON 走旧版兼容层,ZIP 走压缩包解压(Worker 内执行,防大文件卡死/OOM)
    const name = file.name.toLowerCase()
    const isJson = name.endsWith('.json') || file.type.includes('json')
    pendingImport.value = isJson
      ? importFromJson(await file.text())
      : await importFromZipInWorker(file)
    importError.value = ''
    confirmKind.value = 'import'
  } catch (err) {
    pendingImport.value = null
    confirmKind.value = null
    importError.value = err instanceof Error ? err.message : '文件解析失败'
  }
}

/** 替换卡片树后同步运行时态 */
function applyCards(next: Card[]) {
  chatStore.replaceAllCards(next)
}

/**
 * 应用导入包里的**人物顺序**（v7）
 *
 * 为什么必须放在 applyCards **之后**：replaceAllCards 会清空排序记录
 * （旧下标/旧记录与新卡片树无关），所以只有替换完再写顺序才生效。
 *
 * 旧包没有 order 字段时的兜底：按 cards 数组本身的顺序当顺序 ——
 * 导出时 cards 就是按"最近活跃在前"排列的，所以这个兜底同样能还原原顺序。
 *
 * @param payload 导入包
 * @param deriveFromCards 是否允许用 cards 数组顺序兜底
 */
function applyImportedOrder(payload: ProjectPayload, deriveFromCards: boolean) {
  let order = payload.order
  if ((!order || order.length === 0) && deriveFromCards) {
    // 用卡片的稳定标识序列兜底（与 chatStore.cardActiveKey 同一套规则）
    order = (payload.cards ?? []).map((c) => {
      const members = c.members ?? []
      if (members.length >= 2) return `g:${[...members].sort().join('|')}`
      return `s:${c.conversations[0]?.name ?? ''}`
    })
  }
  if (order && order.length) chatStore.applyCardOrder(order)
}

/** 应用导入包里的全局设置（合并/覆盖两种模式共用） */
function applyImportedSettings(payload: ProjectPayload) {
  chatStore.setMyGender(payload.myGender ?? 'male')
  chatStore.setStripVariant(payload.stripVariantIndex ?? 0)
  // v6: 优先应用完整设置快照(API配置/提示词覆盖/think/智能总结/公告标记)
  if (payload.settings && typeof payload.settings === 'object') {
    settingsStore.applySettingsSnapshot(payload.settings)
  }
  // 兼容旧包:无 settings 快照时,单独恢复自定义提示词覆盖
  if (payload.promptOverrides) {
    settingsStore.promptOverrides = { ...payload.promptOverrides }
  }
}

/**
 * 覆盖导入：用导入的卡片**替换**全部本地数据（原行为）
 */
function onConfirmOverwrite() {
  const payload = pendingImport.value
  if (!payload) return
  applyCards(payload.cards)
  // 覆盖导入 → **应用**导入包里的顺序（必须在 applyCards 之后，见函数注释）
  applyImportedOrder(payload, true)
  applyImportedSettings(payload)
  flushPendingWrites()
  confirmKind.value = null
  pendingImport.value = null
  emit('close')
}

/**
 * 合并导入（2026-09-30 新增）
 *
 * 规则见 utils/importMerge.ts：同角色已有对话 → 追加为新的对话卡；
 * 本地无该角色 → 新建；本地同名角色为空 → 直接写入；群聊 → 直接追加。
 */
async function onConfirmMerge() {
  const payload = pendingImport.value
  if (!payload || isMergingImport.value) return
  isMergingImport.value = true
  try {
    // 让出一次绘制，避免大包合并时弹窗"卡住不动"
    await new Promise<void>((r) => setTimeout(r, 16))
    // 合并**忽略**导入顺序、保留本地顺序：先把当前顺序抓下来，
    // 因为 applyCards(→replaceAllCards) 会清空排序记录。
    const localOrderBeforeMerge = chatStore.exportCardOrder()
    const { cards: merged, stats } = mergeCards(chatStore.cards as Card[], payload.cards)
    applyCards(merged)
    // 合并导入 → **忽略**导入包里的顺序，保留本地已有顺序
    // （applyCards 会清空排序记录，所以这里把"合并前"的本地顺序原样写回）
    chatStore.applyCardOrder(localOrderBeforeMerge)
    applyImportedSettings(payload)
    flushPendingWrites()
    exportInfo.value = describeMerge(stats)
    importError.value = ''
  } catch (err) {
    importError.value = err instanceof Error ? err.message : '合并失败'
  } finally {
    isMergingImport.value = false
    confirmKind.value = null
    pendingImport.value = null
    emit('close')
  }
}

/**
 * 四个危险按钮 → 服务器 flag
 *
 * 语义必须和按钮名字对得上（后端按这三个 flag 删不同的表）：
 *   删除全部对话   → 会话全删（消息/摘要/群成员跟着走）
 *   清空全部消息   → 只删消息，会话留着（后端把 next_seq 归零）
 *   清空全部上下文 → 摘要 + 干员记忆 + 会话上的副本
 *   一键清空       → 消息 + 上下文（= 服务端的"历史与上下文都没了"）
 */
function serverFlagsFor(kind: Exclude<ConfirmKind, null | 'import'>) {
  switch (kind) {
    case 'clear':
      return { conversations: true, messages: false, context: false }
    case 'clearMessages':
      return { conversations: false, messages: true, context: false }
    case 'clearContext':
      return { conversations: false, messages: false, context: true }
    case 'clearEverything':
      return { conversations: false, messages: true, context: true }
  }
}

/** 本机那一半（与改动前逐字一致，只是从 onConfirm 里抽出来） */
function applyLocalClear(kind: Exclude<ConfirmKind, null | 'import'>) {
  if (kind === 'clear') {
    chatStore.clearAllConversations()
  } else if (kind === 'clearMessages') {
    chatStore.clearAllMessages()
  } else if (kind === 'clearContext') {
    chatStore.clearAllContext()
  } else if (kind === 'clearEverything') {
    // 一键清空:先清消息(同步进 contextHistory),再清上下文记忆
    chatStore.clearAllMessages()
    chatStore.clearAllContext()
  }
  flushPendingWrites()
}

/**
 * 确认执行清理
 *
 * 顺序是**先服务器、后本机**，故意如此：
 *   反过来的话，本机已经清空、服务器失败 → 用户看到一片空白，以为清干净了，
 *   换台设备一登录数据全回来 —— 这正是用户这次报的那个 bug 的加强版。
 *   现在服务器失败就**一个字都不动**，并明确说"服务器没清掉"。
 */
async function onConfirm() {
  const kind = confirmKind.value
  // 'import' 不在这里执行（它有自己的"合并/覆盖"两个按钮，见上面两个函数）
  if (!kind || kind === 'import') return
  if (clearing.value) return

  clearing.value = true
  clearError.value = ''
  clearResult.value = null

  let server: DataClearResult | null = null
  if (serverClearEnabled.value) {
    try {
      server = await clearServerData(serverFlagsFor(kind))
    } catch (err) {
      // 服务器没清掉：本机也不清（避免"看起来清了、换设备又回来了"）
      const reason = err instanceof Error && err.message ? err.message : '网络或服务端错误'
      clearError.value = `服务器没清掉：${reason}。本机数据也保持原样，稍后重试。`
      clearing.value = false
      return
    }
  }

  applyLocalClear(kind)
  confirmKind.value = null
  pendingImport.value = null
  clearing.value = false
  clearResult.value = { title: confirmTitles[kind], local: true, server }

  // 会话在服务器上被删光了：本地"角色 → 会话 id"的绑定必须一起忘掉，
  // 否则下一句话还会往那个已经不存在的会话里发（后端回 404 no_conversation）。
  if (server && kind === 'clear') forgetAllBindings()

  // 服务端列表模式下顺手重拉一次：不清缓存的话，切回列表还画着刚删掉的会话
  if (server && listStore.source === 'server') void listStore.refresh()
}

function onCancelConfirm() {
  // 正在清理时不给关：请求已经发出去了，这时候关掉只会让用户以为取消了
  if (clearing.value) return
  confirmKind.value = null
  pendingImport.value = null
}
</script>

<template>
  <Transition name="dm">
    <div
      v-if="open"
      class="dm"
      :class="{ 'dm--embedded': embedded }"
      @click.self="emit('close')"
    >
      <div class="dm__panel">
        <!-- 右上角 × 关闭按钮(内嵌模式不显示) -->
        <button v-if="!embedded" class="dm__close" type="button" aria-label="关闭" @click="emit('close')">×</button>
        <h2 v-if="!embedded" class="dm__title">数据管理</h2>
        <p class="dm__stats">
          干员 {{ stats.cardCount }} · 对话 {{ stats.convCount }} · 消息 {{ stats.msgCount }} · 数据
          {{ stats.sizeKB }} KB
        </p>

        <!-- 主页面:导出/导入 + 三个全局清空按钮 -->
        <template v-if="!confirmKind">
          <h3 v-if="embedded" class="dm__group">数据</h3>
          <div class="dm__actions">
            <button
              class="dm__btn dm__btn--primary"
              type="button"
              :disabled="isExporting || isExportingJson"
              @click="onExport"
            >
              {{ exportButtonLabel }}
            </button>
            <!-- 旧版兼容:导出为单文件 JSON -->
            <button
              class="dm__btn"
              type="button"
              :disabled="isExportingJson || isExporting"
              @click="onExportJson"
            >
              {{ exportJsonButtonLabel }}
            </button>
            <!-- 应急导出：只在安卓出现。免 root / 免数据线，直接写进手机下载目录 -->
            <button
              v-if="androidNative"
              class="dm__btn dm__btn--emergency"
              type="button"
              :disabled="isExportingEmergency || isExporting || isExportingJson"
              @click="onEmergencyExport"
            >
              {{ isExportingEmergency ? '应急导出中…' : '应急导出到手机' }}
            </button>
            <button class="dm__btn" type="button" @click="onPickFile">导入数据</button>
          </div>
          <!-- 导出进度：阶段 + 已用秒数（以前这里什么都没有，掉进黑箱） -->
          <p v-if="exportKind" class="dm__info dm__progress-note">
            {{ exportStatusText }}
          </p>

          <!-- 从旧版导入：新客户端的数据目录独立，旧存档要用户自己选（只读） -->
          <h3 v-if="embedded" class="dm__group">从旧版导入</h3>
          <p class="dm__subtitle">
            选旧客户端的 endfield-baker-data.json（只读，不改原文件）；聊天记录会迁到当前账号/设备。
          </p>
          <div class="dm__actions">
            <button
              class="dm__btn"
              type="button"
              :disabled="legacyImporting"
              @click="onPickLegacyFile"
            >
              {{ legacyImporting ? '旧版导入中…' : '从旧版导入…' }}
            </button>
          </div>
          <div v-if="legacyImporting" class="dm__progress">
            <div class="dm__progress-track">
              <div
                class="dm__progress-bar"
                :class="{ 'dm__progress-bar--indeterminate': legacyPercent < 0 }"
                :style="legacyPercent >= 0 ? { width: legacyPercent + '%' } : undefined"
              ></div>
            </div>
            <div class="dm__progress-text"><span>{{ legacyNote }}</span></div>
          </div>
          <p v-else-if="legacyNote" class="dm__info">{{ legacyNote }}</p>
          <p v-if="legacyError" class="dm__error">{{ legacyError }}</p>
          <input
            ref="legacyFileInput"
            type="file"
            accept=".json,application/json"
            hidden
            @change="onLegacyFileChange"
          />

          <div class="dm__divider" v-if="!embedded"></div>

          <!-- 云同步（取件码）：一键上传换码 / 输码领取 -->
          <h3 v-if="embedded" class="dm__group">云同步</h3>
          <p class="dm__subtitle">云同步（取件码 · 24 小时内有效 · 可多台设备重复领取）</p>
          <div class="dm__actions">
            <button class="dm__btn dm__btn--primary" type="button" :disabled="cloudBusy" @click="onCloudUpload">
              {{ cloudBusy ? '上传中…' : '一键上传数据' }}
            </button>
            <button class="dm__btn" type="button" :disabled="cloudFetching" @click="toggleCloudFetch">
              {{ cloudFetchOpen ? '取消' : '拉取历史数据' }}
            </button>
          </div>

          <!-- 进度条:上传 / 验码 / 领取时显示当前进度、速度、已用时间与预计剩余 -->
          <div v-if="cloudStage !== 'idle'" class="dm__progress">
            <div class="dm__progress-track">
              <div
                class="dm__progress-bar"
                :class="{ 'dm__progress-bar--indeterminate': cloudPercent < 0 }"
                :style="cloudPercent >= 0 ? { width: cloudPercent + '%' } : undefined"
              ></div>
            </div>
            <div class="dm__progress-text">
              <span class="dm__progress-phase">
                {{ cloudProgress?.waitingServer ? '服务端'
                   : cloudStage === 'packing' ? '打包'
                   : cloudStage === 'upload' ? '上传'
                   : cloudStage === 'verify' ? '验码' : '领取' }}
              </span>
              <span>{{ cloudProgressText }}</span>
            </div>
          </div>

          <!-- 上传成功：取件码（大号、可点复制）+ 有效期 + 使用说明 -->
          <div v-if="cloudResult" class="dm__code">
            <span class="dm__code-label">取件码</span>
            <code class="dm__code-value" @click="onCopyCloudCode">{{ cloudResult.code }}</code>
            <button class="dm__btn dm__btn--tiny" type="button" @click="onCopyCloudCode">复制</button>
            <span class="dm__code-hint">
              24 小时内可在任意设备领取，可重复领取（多台设备都能各领一份）。
              有效期到 {{ formatExpiresAt(cloudResult.expires_at) }}。
            </span>
            <span class="dm__code-hint">
              在另一台设备上：注册时把取件码填进「设置 → 账号 → 注册 → 取件码（可选）」，
              注册成功后数据就直接落到那个账号；已经登录的设备也可以直接点「拉取历史数据」。
            </span>
          </div>

          <!-- 未登录：不让输码，直接指去账号页（注册时填码最省事） -->
          <div v-if="cloudFetchOpen && accountStore.state === 'unknown'" class="dm__gate">
            <p class="dm__gate-text">
              正在确认登录状态…（如果一直停在这里，说明连不上后端）
            </p>
            <button
              class="dm__btn"
              type="button"
              :disabled="accountStore.busy"
              @click="accountStore.refresh()"
            >重试</button>
          </div>

          <div v-else-if="cloudFetchOpen && !isLoggedIn" class="dm__gate">
            <p class="dm__gate-text">
              领取历史数据要先登录：去「设置 → 账号」注册，注册时把取件码填进
              「取件码（可选）」，注册成功后数据就直接落到这个账号名下。
            </p>
            <button class="dm__btn dm__btn--primary" type="button" @click="emit('goto-account')">
              去账号页注册
            </button>
          </div>

          <!-- 已登录：输入取件码 → 验码 → 领取 -->
          <div v-else-if="cloudFetchOpen" class="dm__fetch">
            <input
              :value="cloudCodeInput"
              class="dm__input"
              :class="{ 'dm__input--invalid': codeInvalid }"
              type="text"
              inputmode="text"
              autocapitalize="characters"
              autocomplete="off"
              spellcheck="false"
              :maxlength="CODE_MAX_INPUT"
              placeholder="输入 8 位取件码"
              @input="onCodeInput"
              @blur="onCodeBlur"
              @keyup.enter="onCloudFetch"
            />
            <button
              class="dm__btn dm__btn--primary"
              type="button"
              :disabled="cloudFetching || !codeValid"
              @click="onCloudFetch"
            >
              {{ cloudFetching ? '领取中…' : '验证并领取' }}
            </button>
            <!-- 只在格式错误时提示；正常情况不占任何高度，保持界面干净 -->
            <p v-if="codeInvalid" class="dm__fetch-error">取件码只能是大写字母和数字（共 8 位）</p>
          </div>

          <!-- 领取成功：导入摘要（几段会话 / 几条消息 / 几张图 / 跳过了什么） -->
          <div v-if="claimResult" class="dm__claim">
            <p class="dm__claim-title">领取成功</p>
            <p class="dm__claim-line">{{ claimSummary }}</p>
            <ul v-if="claimResult.notes.length" class="dm__claim-notes">
              <li v-for="(note, i) in claimResult.notes" :key="i">{{ note }}</li>
            </ul>
            <p class="dm__claim-tip">
              数据已经在这个账号名下：之后和对应干员聊天时会自动接上这段历史。
              （左侧列表显示的是这台设备的本地存档，导入的历史不会单独列成一张卡。）
            </p>
          </div>

          <div class="dm__divider" v-if="!embedded"></div>

          <h3 v-if="embedded" class="dm__group dm__group--danger">危险操作</h3>
          <div class="dm__actions dm__actions--menu">
            <button class="dm__btn dm__btn--danger" type="button" :disabled="clearing" @click="onRequestClear">删除全部对话</button>
            <button class="dm__btn" type="button" :disabled="clearing" @click="onRequestClearMessages">清空全部消息</button>
            <button class="dm__btn" type="button" :disabled="clearing" @click="onRequestClearContext">清空全部上下文</button>
            <!-- 一键清空:同时执行清空全部消息 + 清空全部上下文(AI 不再记得之前对话) -->
            <button class="dm__btn dm__btn--danger" type="button" :disabled="clearing" @click="onRequestClearEverything">一键清空历史与上下文</button>
          </div>

          <!--
            清理结果（2026-11-18）：本机与服务器分开报。
            只写"已清空"是不够的 —— 用户这次就是被"看起来清了、服务器还在"坑到的。
          -->
          <div v-if="clearResult" class="dm__clear-result">
            <p class="dm__clear-title">{{ clearResult.title }}完成</p>
            <p class="dm__clear-line">本机已清。</p>
            <p v-if="clearResult.server" class="dm__clear-line">{{ describeServerClear(clearResult.server) }}。</p>
            <p v-else class="dm__clear-line">这次只清了本机（没动服务器上的数据）。</p>
          </div>
        </template>

        <!-- 导出成功后的"分享文件"：落在公共下载目录时不需要，但留着也无害 -->


        <p v-if="clearError || importError || cloudError" class="dm__error">{{ clearError || cloudError || importError }}</p>
        <p v-else-if="exportInfo" class="dm__info">{{ exportInfo }}</p>
        <p v-if="cloudInfo && !cloudError && !importError" class="dm__info">{{ cloudInfo }}</p>

        <input ref="fileInput" type="file" :accept="IMPORT_FILE_ACCEPT" hidden @change="onFileChange" />
      </div>

      <!--
        破坏性操作的二次确认:与公告 / 提醒 / 更新弹窗同一副外壳。
        以前是"把面板内容换成确认页",玩家会失去上下文;现在改成覆盖式弹窗,
        背景里的数据页还在,取消即原样返回。
      -->
      <VersionDialogShell
        :open="!!confirmKind"
        :title="confirmTitle"
        kind="alert"
        :closable="!isMergingImport && !clearing"
        @close="onCancelConfirm"
      >
        <!-- 导入:让用户选「合并」还是「覆盖」 -->
        <template v-if="confirmKind === 'import'">
          <p class="dm__confirm-text">导入方式:合并保留现有数据，覆盖会替换全部数据。</p>
          <p class="dm__confirm-note">
            合并规则：同一角色已有对话时，导入的对话会作为新的对话卡追加到该角色下；
            本地没有的角色则直接新建；群聊一律直接追加。
          </p>
        </template>
        <p v-else-if="confirmKind" class="dm__confirm-text">{{ confirmTexts[confirmKind] }}</p>
        <!-- 服务器那一句：只有真会打电话给服务器时才出现（纯本机用户看不见，文案不变） -->
        <p v-if="confirmKind && confirmKind !== 'import' && confirmServerNote" class="dm__confirm-note dm__confirm-note--server">
          {{ confirmServerNote }}
        </p>

        <template #actions>
          <template v-if="confirmKind === 'import'">
            <button class="pn-btn" type="button" :disabled="isMergingImport" @click="onConfirmMerge">
              <span class="pn-btn__tex" aria-hidden="true"></span>
              <span class="pn-btn__deco" aria-hidden="true"></span>
              <span class="pn-btn__label">{{ isMergingImport ? '合并中…' : '合并数据' }}</span>
            </button>
            <button class="pn-btn dm-act--danger" type="button" :disabled="isMergingImport" @click="onConfirmOverwrite">
              <span class="pn-btn__label">覆盖数据</span>
            </button>
            <button class="pn-btn dm-act--ghost" type="button" :disabled="isMergingImport" @click="onCancelConfirm">
              <span class="pn-btn__label">取消</span>
            </button>
          </template>
          <template v-else>
            <button class="pn-btn dm-act--danger" type="button" :disabled="clearing" @click="onConfirm">
              <span class="pn-btn__label">{{ clearing ? '清理中…' : '确认' }}</span>
            </button>
            <button class="pn-btn dm-act--ghost" type="button" :disabled="clearing" @click="onCancelConfirm">
              <span class="pn-btn__label">取消</span>
            </button>
          </template>
        </template>
      </VersionDialogShell>
    </div>
  </Transition>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;
@use '../../styles/mixins' as *;

// 提示小字统一配色（2026-09-30 按用户要求改为**白色**）
//   云同步说明（“最多保留 24~48 小时”那行）/ 进度信息 / 结果提示都用它；
//   错误行仍是红色。
//   注意：进度条相关文字另有专门的黄色（$progress-text-color），见下。
$hint-text-color: #ffffff;

// 进度条 & 进度文字统一**黄色**（用户要求：底下的蓝色进度条和蓝色提示字都改黄）
//   进度条本体见 .dm__progress-bar；这里给文字与阶段徽标用同一套金色。
$progress-text-color: #ffef00;

// ---- 云同步区块 ----
// 2026-09-30 修复:原先没有声明 color,靠继承 —— 深色父级下这行说明会显示成黑色,
// 与其它提示小字(#9fe0ff 淡蓝)风格不一致。显式指定同一套配色。
.dm__subtitle {
  margin: 0 0 8px;
  font-size: 13px;
  color: $hint-text-color;
  // 深色底上 0.75 的白字偏灰、观感发闷；提到 0.9 与主字体一致清晰
  opacity: 0.9;
}
.dm__code {
  margin: 8px 0 4px;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.06);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.dm__code-label {
  font-size: 13px;
  color: $hint-text-color;
  opacity: 0.9;
}
.dm__code-value {
  // 取件码是这一步唯一的产出：字号给足，隔着屏幕也能抄
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 4px;
  cursor: pointer;
  user-select: all;
  // 原实现**没有声明 color** → 继承成黑色，在深色面板上几乎看不见。
  // 改用主题主文字色（与其他白字同源，见 styles/_variables.scss 的 $color-text-primary）。
  color: $color-text-primary;
}
.dm__code-hint {
  flex: 1 0 100%;
  font-size: 12px;
  line-height: 1.5;
  color: $hint-text-color;
}
// 未登录时的"去注册"提示块：整块淡黄底，与普通说明区分开
.dm__gate {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  margin: 8px 0 4px;
  padding: 10px 12px;
  border-radius: 10px;
  border-left: 3px solid #ffef00;
  background: rgba(255, 239, 0, 0.08);
}
.dm__gate-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: $hint-text-color;
}
// 领取结果：绿色系（与错误红字明确分开）
.dm__claim {
  margin: 8px 0 4px;
  padding: 10px 12px;
  border-radius: 10px;
  border-left: 3px solid #6be39a;
  background: rgba(107, 227, 154, 0.1);
}
.dm__claim-title {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 700;
  color: #6be39a;
}
.dm__claim-line {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: $color-text-primary;
}
.dm__claim-notes {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.6;
  color: $hint-text-color;
}
.dm__claim-tip {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: $hint-text-color;
  opacity: 0.85;
}
.dm__fetch-error {
  flex: 1 0 100%;
  margin: 2px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: #ff8f8f;
}

// 危险操作的结果（2026-11-18）：本机/服务器分开两行，绿框 = 真的清掉了。
// 配色沿用上面「领取成功」那块，用户已经认得"绿色 = 成功"。
.dm__clear-result {
  margin: 10px 0 4px;
  padding: 10px 12px;
  border-radius: 10px;
  border-left: 3px solid #6be39a;
  background: rgba(107, 227, 154, 0.1);
}
.dm__clear-title {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 700;
  color: #6be39a;
}
.dm__clear-line {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: $hint-text-color;
}
.dm__fetch {
  display: flex;
  gap: 8px;
  margin: 8px 0 4px;

  // 窄屏（手机）适配：原来是单行 flex —— 输入框 `flex:1; min-width:0`，
  // 旁边还要挤出按钮，结果输入框被压成很细的一条。
  // 改为**上下堆叠**：输入框独占整行（能完整显示出 8 位取件码），按钮在下一行。
  // 断点用 768px —— 与项目统一的 MOBILE_BREAKPOINT（composables/useMobile.ts）一致。
  @media (max-width: 768px) {
    flex-direction: column;
    align-items: stretch;

    .dm__btn {
      width: 100%;
    }
  }
}
.dm__input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  background: rgba(0, 0, 0, 0.25);
  // 原来写 `color: inherit` → 在深色弹窗里继承成黑色，输入内容几乎看不见。
  // 改为主题主文字色（与页面内其他白字同源）。
  color: $color-text-primary;
  font-size: 16px;          // ≥16px 可避免 iOS Safari 聚焦时自动放大页面
  letter-spacing: 2px;
  text-transform: uppercase;
  // 手机上留足触控高度，且取件码不会因为宽度不够而看不见
  min-height: 40px;
  box-sizing: border-box;

  &::placeholder {
    color: rgba(255, 255, 255, 0.45);
  }

  // 校验不通过：红框（与提示红字同色系）
  &--invalid {
    border-color: #ff8f8f;
    background: rgba(255, 143, 143, 0.08);
  }
}
.dm__btn--tiny {
  padding: 4px 10px;
  font-size: 12px;
}
// 导出后的"分享文件"一行
.dm__share {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin: 8px 0 2px;
}
// 应急导出:和普通按钮区分开(橙黄,提示"这是出问题时的救命按钮")
.dm__btn--emergency {
  background: #b4761f;
  color: #fff8ec;
}

// ---- 进度条(上传 / 取回) ----
.dm__progress {
  margin: 10px 0 6px;
}
.dm__progress-track {
  position: relative;
  height: 8px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.12);
}
.dm__progress-bar {
  height: 100%;
  width: 0;
  border-radius: 6px;
  // 2026-09-30：改为**按钮同款亮黄**（= $color-subcard-selected #ffef00）。
  // 之前用的金色渐变偏暗，和界面上的强调黄不一致。
  background: linear-gradient(90deg, #ffef00, #ffe95c);
  transition: width 0.18s linear;
}
// 总量未知(服务器未给 Content-Length)时走来回滚动动画
.dm__progress-bar--indeterminate {
  width: 35% !important;
  animation: dm-progress-slide 1.1s ease-in-out infinite;
}
@keyframes dm-progress-slide {
  0% {
    margin-left: -35%;
  }
  100% {
    margin-left: 100%;
  }
}
.dm__progress-text {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: $progress-text-color;
  font-variant-numeric: tabular-nums;
}
.dm__progress-phase {
  padding: 0 6px;
  border-radius: 4px;
  // 徽标底色同样亮黄（原来是蓝色 rgba(110,210,255,.18)）
  background: rgba(255, 239, 0, 0.18);
  color: $progress-text-color;
}
// 诊断行:原生组件状态(不可用时一眼看出问题在哪)
.dm__progress-diag {
  flex: 1 0 100%;
  color: $progress-text-color;
  opacity: 0.9;
}


// 数据管理弹窗:半透明遮罩 + 居中深色面板(CSS 绘制,不新增素材图)
@include dialog-shell(dm, 500px, 14px, 'stats', true);

// 内嵌模式(设置弹窗内):去掉遮罩/居中定位,面板铺满容器宽度
.dm {
  &--embedded {
    position: static;
    inset: auto;
    display: block;
    background: transparent;
    z-index: auto;

    .dm__panel {
      width: 100%;
      max-width: none;
      padding: 0;
      border: none;
      box-shadow: none;
      background: transparent;
    }
  }

  &__panel--narrow {
    width: 348px;
  }

  &__confirm-text {
    margin: 0 0 14px;
    font-family: $font-harmony;
    font-size: 16px;
    color: $color-subcard-text;
  }

  &__divider {
    height: 1px;
    margin: 14px 0;
    background: rgba(255, 255, 255, 0.1);
  }

  &__error {
    margin: 12px 0 0;
    font-family: $font-harmony;
    font-size: 14px;
    color: #ff8f8f;
  }

  &__info {
    margin: 12px 0 0;
    font-family: $font-harmony;
    font-size: 13px;
    color: $hint-text-color;
  }

  &__actions--menu {
    flex-direction: column;
    gap: 10px;
  }

  &__btn--danger {
    background: #7a2e2e;
    color: #f0eeee;
  }
}

// ---- 二次确认弹窗(VersionDialogShell 的白底正文区) ----------------------
// 弹窗正文是浅色底,原先那套深色面板用的浅灰字在这里会看不清,单独覆盖一档。
.dm__confirm-text {
  margin: 0 0 10px;
  color: #1f1f1f;
  font-family: $font-harmony;
  font-size: 14px;
  line-height: 1.75;
}

.dm__confirm-note {
  margin: 0;
  color: #5f5f5f;
  font-family: $font-harmony;
  font-size: 13px;
  line-height: 1.75;
}

// 「会同时删除服务器上的数据」这一句：确认弹窗正文是白底，
// 所以用深红而不是浅色（浅色在白底上等于看不见）。
.dm__confirm-note--server {
  margin-top: 6px;
  color: #9a2b2b;
  font-weight: 600;
}

// 弹窗按钮:危险操作红底、次要操作浅底,主操作沿用 .pn-btn 的深色
.dm-act--danger {
  background: #7a2e2e;
  color: #f0eeee;
  box-shadow: 0 0 0 0.05em #7a2e2e, 0 0 0 0.05em #a86a6a inset;

  &:hover:not(:disabled) {
    background: #632424;
    color: #f8f4f4;
    box-shadow: 0 0 0 0.05em #632424, 0 0 0 0.05em #a86a6a inset;
  }
}

.dm-act--ghost {
  background: rgba(0, 0, 0, 0.06);
  color: #2a2a2a;
  box-shadow: 0 0 0 1.5px rgba(0, 0, 0, 0.18) inset;

  &:hover:not(:disabled) {
    background: rgba(0, 0, 0, 0.12);
    color: #111;
  }
}
</style>
