<script setup lang="ts">
// =============================================================================
// 设置弹窗(SettingsDialog)
// -----------------------------------------------------------------------------
// AI API 配置 + 提示词编辑,全部持久化到 localStorage(settings store)
// 打开/关闭状态由父组件 App 持有
// =============================================================================
import { ref, computed, watch } from 'vue'
import { useSettingsStore } from '../../stores/settings'
import { testApiConnection } from '../../utils/llm'
import { testBackendConnection, forgetAllBindings } from '../../utils/backend'
import {
  CLAIM_CODE_LEN,
  claimAfterVerify,
  describeClaimResult,
  fetchLatestImport,
  isClaimCodeValid,
  normalizeClaimCode,
} from '../../utils/cloudData'
import { useModelPicker } from '../../composables/useModelPicker'
import { guessContextWindow } from '../../utils/modelContext'
import {
  FALLBACK_CONTEXT_WINDOW,
  DEFAULT_COMPACT_TRIGGER_PERCENT,
  MIN_COMPACT_TRIGGER_PERCENT,
  MAX_COMPACT_TRIGGER_PERCENT,
  clampTriggerPercent,
  computeInputBudget,
} from '../../utils/contextBudget'
import { lookupModelContext } from '../../utils/modelsDev'
import type { LibraryHit } from '../../utils/modelsDev'
import {
  clampSummaryTriggerPercent,
  computeSummaryTriggerBounds,
  triggerTokenEstimate,
} from '../../utils/summaryThreshold'
import { summaryPromptTokens } from '../../utils/summaryPromptBudget'
import { formatTokenCount } from '../../utils/usage'
import { useChatStore } from '../../stores/chat'
import { useAccountStore } from '../../stores/account'
import { useListStore } from '../../stores/list'
import {
  PASSWORD_MIN,
  PASSWORD_MAX,
  USERNAME_RULE_TEXT,
  validatePassword,
  validateUsername,
} from '../../utils/auth'
import { countHistoryRounds } from '../../utils/summaryTrim'
import {
  CUSTOM_SUMMARY_FALLBACK_CONTEXT,
  formatCustomSummaryInfoLine,
} from '../../utils/customSummary'
import { CHARACTER_PROMPTS } from '../../constants/prompts'
import {
  API_BASES,
  API_ROUTE_LABELS,
  type ApiRoute,
} from '../../constants/apiRoutes'
import { SETTINGS_NAV } from '../../constants/settingsNav'
import type { SettingsTabKey } from '../../constants/settingsNav'
import { BACKEND_CONTEXT_WINDOW_TOKENS } from '../../constants/summaryBudget'
import DataManagerDialog from './DataManagerDialog.vue'
// 时间感知开关（存 localStorage，发消息时由 utils/backend.ts 读取）
import { readTimeAware, writeTimeAware } from '../../utils/timeAwareness'
// 关于面板(3.2.2):原「免责声明」页重做 —— 头图 / 群二维码 / 更新日志 / 条款入口
import AboutPanel from './AboutPanel.vue'
// 干员记忆面板(跨对话记忆:全局开关 + 「她记得什么」列表 + 新建对话时的本局选择)
import MemoryPanel from './MemoryPanel.vue'
// 面板设计系统的三个小组件(分组标题 / 设置行 / 分段控件)
import SetSection from './ui/SetSection.vue'
import SetRow from './ui/SetRow.vue'
import SegControl from './ui/SegControl.vue'
import { MATERIALS } from '../../constants/materials'

const props = defineProps<{
  /** 是否展开 */
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const settingsStore = useSettingsStore()
const chatStore = useChatStore()
const accountStore = useAccountStore()
// 列表数据源（P1）：领取 / 注册带码之后要刷新一次列表，
// 导入的会话才会"立刻出现在左侧"（见 docs/07 §4 的验收核心）
const listStore = useListStore()

// ===========================================================================
// 账号页（登录 / 注册 / 退出 / 改密）
// ---------------------------------------------------------------------------
// 后端是「设备匿名 + 账号」双轨（web/back/docs/03-登录与身份.md）：
//   · 这台设备一进来就有一张匿名 token，能直接聊天；
//   · 登录只是把这张 token 挂到账号上 —— **不换 token、不重拉会话**。
// 所以这里没有"登录成功后重新初始化"那一步，只更新账号状态。
// ===========================================================================

/** 未登录时的两个页签 */
const accountMode = ref<'login' | 'register'>('login')
/** 登录/注册表单 */
const accountUsername = ref('')
const accountPassword = ref('')
const accountConfirm = ref('')
const accountDisplayName = ref('')
/** 改密表单 */
const accountOldPassword = ref('')
const accountNewPassword = ref('')
const accountNewConfirm = ref('')
/** 本地校验提示（与后端错误分开显示：本地错不用等网络） */
const accountFormError = ref('')

// ---- 取件码（跨设备搬数据用的 8 位码） -------------------------------------
/**
 * 注册时可选填的取件码
 *
 * 场景：旧设备「数据管理 → 一键上传数据」拿到一个 8 位码，新设备注册时填进去，
 * 后端会在建账号的同时把这份包领到这个账号名下（字段 claim_code）。
 */
const accountClaimCode = ref('')
/**
 * 取件码相关的错误（单独一行、红底提示）
 *
 * 为什么单独一个 ref：`bad_claim_code` 意味着"这次注册整个回滚了，账号没建成"，
 * 比普通的表单校验错误严重得多，必须醒目；而且此时**不能清空**用户填的用户名/密码
 * ——他只需要换个码（或去掉码）再点一次注册。
 */
const accountClaimError = ref('')

/** 已登录时「用取件码领取历史数据」的输入 */
const claimEntryCode = ref('')
/** 领取中（按钮禁用） */
const claimBusy = ref(false)
/** 领取失败的一句话 */
const claimError = ref('')
/**
 * 领取结果的摘要（注册时带码 / 已登录手动领取 都用这一条显示）
 *
 * 放在"访客/已登录"两个区块之外：注册成功后页面会从"注册表单"切成"已登录"，
 * 摘要若挂在访客区块里会立刻消失，用户就看不到"我到底领到了什么"。
 */
const claimSummary = ref('')

/**
 * 取件码输入处理：自动大写 + 去空格 + 只留字母数字
 *
 * 用户在另一台设备上看到的是 8 位大写码，抄过来常带空格或小写；
 * 这里即时纠正，避免"看起来输了却提交不了"。
 */
function onClaimCodeInput(raw: Event, target: 'register' | 'entry'): void {
  const el = raw.target as HTMLInputElement
  const cleaned = normalizeClaimCode(el.value)
  if (el.value !== cleaned) el.value = cleaned
  if (target === 'register') {
    accountClaimCode.value = cleaned
    // 用户开始改码 → 上一次"这码不行"的提示已经过时，清掉（红框跟着消失）
    accountClaimError.value = ''
  } else {
    claimEntryCode.value = cleaned
    claimError.value = ''
  }
}

/**
 * 已登录用户领取历史数据（与「数据管理 → 拉取历史数据」同一套接口）
 *
 * 先验码再领取：验码能给"输错了 / 过期了"更准的提示，而且不需要登录；
 * 领取才需要登录（数据要落到账号名下）。
 */
async function onClaimSubmit(): Promise<void> {
  claimError.value = ''
  claimSummary.value = ''
  const code = normalizeClaimCode(claimEntryCode.value)
  if (!isClaimCodeValid(code)) {
    claimError.value = `取件码是 ${CLAIM_CODE_LEN} 位字母或数字，请检查一下`
    return
  }
  claimBusy.value = true
  try {
    const result = await claimAfterVerify(code)
    claimSummary.value = describeClaimResult(result)
    claimEntryCode.value = ''
    // 领取会在服务端新建会话：清掉本地绑定，下一次聊天才接得上刚导入的历史
    forgetAllBindings()
    // P1 验收核心：领完必须"看得见" —— 该切服务端列表就切，然后刷新。
    // 只调 refresh() 是不够的：老用户停在 local 时它是空操作（2026-11-18 复验 FAIL）。
    void listStore.afterClaim()
    void accountStore.refresh(true, true)
  } catch (err) {
    claimError.value = err instanceof Error ? err.message : '领取失败'
  } finally {
    claimBusy.value = false
  }
}

/**
 * 注册时带了取件码 → 注册成功后补一件事：问一次"刚领到什么"并显示出来
 *
 * 为什么不在注册后再调一次 claim：包是**后端在 register 里领的**（所以码错了
 * 整次注册回滚）。前端手里没有那次导入的返回体，只能从导入记录（GET /v1/imports）
 * 里把最新一条捞出来当摘要；捞不到就只显示一句通用提示，不影响"注册成功"。
 */
async function showClaimedAfterRegister(code: string): Promise<void> {
  forgetAllBindings()
  // P1：注册带码同样要"看得见"（后端在 register 里就把包领进来了）
  void listStore.afterClaim()
  void accountStore.refresh(true, true)
  const latest = await fetchLatestImport()
  // 导入记录是**按设备**记的（不是按账号）：这台设备以前可能还有别的导入。
  // 只有当"最近一条"就是刚才这次（5 分钟内）时才敢拿它当摘要显示，
  // 否则只给一句通用提示，不把旧数据说成"刚领到的"。
  const created = latest ? Date.parse(latest.created_at) : Number.NaN
  const fresh = Number.isFinite(created) && Date.now() - created < 5 * 60 * 1000
  claimSummary.value = latest && fresh
    ? `注册时已领取取件码 ${code} 里的数据：${describeClaimResult(latest, '').trim()}`
    : `取件码 ${code} 已随注册一起领取，数据已归到这个账号名下。`
}

/** 用户名规则（与后端同一条） */
const accountUsernameHint = USERNAME_RULE_TEXT
/** 密码提示 */
const accountPasswordHint = `密码 ${PASSWORD_MIN}–${PASSWORD_MAX} 位（不强制大小写/符号）`

/** 登录 / 注册 分段控件的选项 */
const ACCOUNT_MODE_SEG = [
  { value: 'login', label: '登录' },
  { value: 'register', label: '注册' },
]

/** 切换登录 / 注册 */
function switchAccountMode(mode: string): void {
  accountMode.value = mode === 'register' ? 'register' : 'login'
  accountFormError.value = ''
  // 取件码的报错只在"注册"这一步有意义；切页签后不该继续挂着
  accountClaimError.value = ''
  accountStore.clearNotice()
}

/** 清空表单里的密码（用户名保留，方便改错） */
function clearAccountSecrets(): void {
  accountPassword.value = ''
  accountConfirm.value = ''
  accountOldPassword.value = ''
  accountNewPassword.value = ''
  accountNewConfirm.value = ''
}

/** 提交登录 / 注册 */
async function onAccountSubmit(): Promise<void> {
  accountFormError.value = ''
  accountClaimError.value = ''
  accountStore.clearNotice()

  const nameError = validateUsername(accountUsername.value)
  if (nameError) {
    accountFormError.value = nameError
    return
  }
  const passwordError = validatePassword(accountPassword.value)
  if (passwordError) {
    accountFormError.value = passwordError
    return
  }
  if (accountMode.value === 'register') {
    if (accountPassword.value !== accountConfirm.value) {
      accountFormError.value = '两次输入的密码不一致'
      return
    }
    // 取件码可选；填了就必须是 8 位（本地先拦一次，省一次网络往返）
    const claimCode = normalizeClaimCode(accountClaimCode.value)
    if (claimCode && !isClaimCodeValid(claimCode)) {
      accountClaimError.value = `取件码是 ${CLAIM_CODE_LEN} 位字母或数字，请检查一下`
      return
    }
    const result = await accountStore.register(
      accountUsername.value,
      accountPassword.value,
      accountDisplayName.value,
      claimCode,
    )
    if (result.ok) {
      accountClaimCode.value = ''
      // 带了码 → 后端已把包领进新账号；把"领到了什么"显示出来
      if (claimCode) await showClaimedAfterRegister(claimCode)
    } else if (result.code === 'bad_claim_code') {
      // 后端的语义是"码无效/过期 → 整次注册回滚"（账号没建成）。
      // 这里**不清空**用户名/密码：用户换个码就能重试，不用重新敲一遍。
      accountClaimError.value =
        `取件码 ${claimCode} 无效或已过期（有效期 24 小时）：这次没有注册成功，账号没有创建。` +
        '换个取件码，或者清空取件码再注册一次。'
    }
  } else {
    await accountStore.login(accountUsername.value, accountPassword.value)
  }
  if (accountStore.state === 'user') clearAccountSecrets()
}

/** 退出登录 */
async function onAccountLogout(): Promise<void> {
  accountFormError.value = ''
  const result = await accountStore.logout()
  if (result.ok) clearAccountSecrets()
}

/** 修改密码 */
async function onChangePassword(): Promise<void> {
  accountFormError.value = ''
  accountStore.clearNotice()

  if (!accountOldPassword.value) {
    accountFormError.value = '请填写旧密码'
    return
  }
  const passwordError = validatePassword(accountNewPassword.value, '新密码')
  if (passwordError) {
    accountFormError.value = passwordError
    return
  }
  if (accountNewPassword.value !== accountNewConfirm.value) {
    accountFormError.value = '两次输入的新密码不一致'
    return
  }
  if (accountNewPassword.value === accountOldPassword.value) {
    accountFormError.value = '新密码和旧密码不能一样'
    return
  }
  const result = await accountStore.changePassword(
    accountOldPassword.value,
    accountNewPassword.value,
  )
  if (result.ok) clearAccountSecrets()
}

/** 平台名（设备列表） */
function platformLabel(platform: string): string {
  const map: Record<string, string> = {
    electron: 'Windows 客户端',
    capacitor: '安卓 App',
    'android-web': '安卓浏览器',
    'ios-web': 'iOS 浏览器',
    web: '网页',
    unknown: '未知设备',
  }
  return map[platform] ?? platform ?? '未知设备'
}

/** 相对时间（设备"最后活跃"） */
function shortTime(iso: string | null | undefined): string {
  if (!iso) return '未知'
  const time = new Date(iso)
  if (Number.isNaN(time.getTime())) return '未知'
  const diff = Date.now() - time.getTime()
  const minute = 60 * 1000
  if (diff < minute) return '刚刚'
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${time.getFullYear()}-${pad(time.getMonth() + 1)}-${pad(time.getDate())}`
}

/**
 * 时间感知：角色知不知道"现在几点"（默认开）
 *
 * 关掉 ≠ 让它别报时：后端会**完全不注入时间**，并明确告诉模型"你没有时间信息"，
 * 被问到只能说不知道。存 localStorage（key `baker_time_aware`），
 * 发消息时由 utils/backend.ts / groupBackend.ts 直接读 —— 见 utils/timeAwareness.ts。
 */
const timeAware = ref(readTimeAware())

/** 切换时间感知：立刻落盘（发下一条消息就生效，不需要保存按钮） */
function onTimeAwareChange(value: string): void {
  timeAware.value = value === 'on'
  writeTimeAware(timeAware.value)
}

/** 当前标签页(键与 SETTINGS_NAV 一致;提示词与背景已并入 prompts 页的"自定义 API"分支) */
const activeTab = ref<SettingsTabKey>('connection')

/**
 * 页签兜底（4.0.5）
 *
 * 「干员记忆」在 4.0.5 起暂时关闭，SETTINGS_NAV 里已经没有这一项。
 * 但页签键可能来自旧状态 / 外部入口（比如之前打开的 memory 页），
 * 这时主区会白屏（标题回落到「设置」、面板又不渲染）。统一落回第一页。
 */
watch(activeTab, (tab) => {
  if (!SETTINGS_NAV.some((item) => item.key === tab)) {
    activeTab.value = SETTINGS_NAV[0]?.key ?? 'connection'
  }
}, { immediate: true })

/** 当前标签页名称:主区标题用它(侧边栏收起时也要能一眼看出在哪一页) */
const activeLabel = computed(
  () => SETTINGS_NAV.find(t => t.key === activeTab.value)?.label ?? '设置',
)

/** 提示行语气(与 SetRow 的 tone 一一对应:成功 / 警示 / 中性) */
type HintTone = 'ok' | 'warn' | 'keep'

/**
 * 布尔开关的分段控件选项
 *
 * 设置页里绝大多数开关都只有两档,统一用这一份选项表:
 * 分段控件只认字符串值,布尔状态由调用方映射成 'on' / 'off'。
 */
const BOOL_SEG = [
  { value: 'on', label: '开启' },
  { value: 'off', label: '关闭' },
]

/** 接口模式二选一（默认 / 自定义）→ switchApiMode(值来自分段控件的字符串) */
function onApiModeChange(value: string): void {
  if (value === 'backend' || value === 'custom') switchApiMode(value)
}

/** 后端路线(默认 / 备用两条域名) */
function onApiRouteChange(value: string): void {
  const hit = API_ROUTE_OPTIONS.find(o => o.value === value)
  if (hit) settingsStore.setApiRoute(hit.value)
}

/**
 * 列表数据源切换（2026-11-20 本地优先）
 *
 * local  = 只读这台设备的存档（默认，不用登录）
 * server = 再和云端合并一次：云端有、本机没有的会话补进来（本机一条都不删）
 *
 * 注意：不管选哪个，本机**都有一份完整副本**（写本地不再受数据源影响）。
 */
function onListSourceChange(value: string): void {
  listStore.setSource(value === 'server' ? 'server' : 'local')
}

/** 列表数据源这一行的提示文案 */
const listSourceHint = computed<{ text: string; level: HintTone }>(() => {
  // 刚刚点过但被拒：先说清原因（这条只存在内存里，没有写任何存储）
  if (listStore.rejectedReason) return { text: listStore.rejectedReason, level: 'warn' }
  if (listStore.blockedReason) return { text: listStore.blockedReason, level: 'warn' }
  if (listStore.syncNotice) return { text: listStore.syncNotice, level: 'ok' }
  if (listStore.source === 'server') {
    if (listStore.offline) return { text: '云端暂时连不上：照旧读本机存档', level: 'warn' }
    return { text: '已开启云端同步：本机记录照旧，云端独有的会话会补进来', level: 'ok' }
  }
  return { text: '记录存在这台设备上（不用登录）；登录后会自动上传到服务器', level: 'keep' }
})

/** 总结 API 模式(默认 / 自定义) */

/** 内容滚动容器(切页时回到顶部用) */
const bodyEl = ref<HTMLElement | null>(null)

/**
 * 切换标签页
 *
 * 换页后把内容区滚回顶部:各页高度差很大(API 页很长、关于页很短),
 * 沿用上一页的滚动位置会落在半空中。
 */
function onSelectTab(key: SettingsTabKey) {
  if (activeTab.value === key) return
  activeTab.value = key
  if (bodyEl.value) bodyEl.value.scrollTop = 0
}

// ---- API 配置本地缓存(打开时同步,保存时写入 store) -------------------------
// 初始值必须直接取 store：旧实现给的是 apiMode:'custom' 的占位值，
// 弹窗还没打开时 setup 里的「精细度自动夹取」就会按 custom 的 32768 窗口
// 算出 78% 并写进 localStorage，等用户打开设置（默认 API / 131072）时
// 阈值已经被悄悄改成 78 了。这里与 store 对齐，避免启动即改用户设置。
const apiDraft = ref({ ...settingsStore.apiConfig })


/** 是否为自定义 API 模式(需要显示 baseUrl/apiKey/model 输入框) */
const isCustomMode = computed(() => apiDraft.value.apiMode === 'custom')

/**
 * 各实验性开关"在当前配置下不生效"的判据
 * (2026-10-04 按各链路实际能力重新划分)
 *
 * 背景:这些开关以前只有「默认 API 模式」与「群聊(走后端群聊服务)」会读,
 * 所以共用一条判据置灰。自定义 API 模式补上群聊链路、并补齐直连的
 * 思考模式 / 沉浸式之后,四项的能力边界各不相同,必须分开判断 ——
 * 否则会把"其实已经生效"的开关误灰(用户根本拨不动)。
 *
 *   - 沉浸式         :两种模式 + 单聊/群聊都已落实(后端过滤或前端本地过滤)→ 永不置灰
 *   - 思考模式       :默认 API 全场景、自定义 API(单聊 think 透传 / 群聊生成)
 *                     都生效 → 永不置灰（原来"旧版直连 + 单聊"不支持，该模式已下线）
 *   - 使用新版提示词 :只有后端会按 characters_v2/ 加载 → 仅默认 API 模式生效
 *   - 智能总结开关   :默认模式由服务端自动压缩（开关只对自定义 API 生效）
 */
const newPromptBlocked = computed(() => apiDraft.value.apiMode !== 'backend')
// 自定义 API 模式的总结由前端直连用户端点生成；「智能总结」开关不按模式置灰。
const summaryBlocked = computed(() => false)

/**
 * 默认模式（`/v1`）下总结/压缩全在服务端做
 *
 * 依据：`web/back/app/memory.py` 的 `compress_to_fit()`（每轮生成前、单聊群聊都走）
 * 与 `app/prompt/budget.py`（200k 预算，80% 触发、压到 50%）。
 * 前端这套「智能总结」是**老客户端/自定义 API** 才需要的东西 ——
 * 在默认模式下它只会造成"两条压缩链打架"（见 docs/08），
 * 所以设置里保留开关（切到自定义 API 还要用），但**说明要写清楚**，并且把只对
 * 前端链路有意义的滑条藏起来，别让用户以为要自己调。
 */
const summaryServerManaged = computed(() => apiDraft.value.apiMode === 'backend')

/** 新版提示词禁用时的说明 */
const NEW_PROMPT_BLOCKED_NOTE = '仅「默认 API」模式可用'
/** 智能总结开关禁用时的说明（保留常量：当前没有模式会置灰） */
const SUMMARY_BLOCKED_NOTE = '当前模式下不生效'

/** 受限开关的悬浮提示(禁用时说明原因,可用时不给多余提示) */
const newPromptTitle = computed(() => (newPromptBlocked.value ? NEW_PROMPT_BLOCKED_NOTE : undefined))
const summaryTitle = computed(() => (summaryBlocked.value ? SUMMARY_BLOCKED_NOTE : undefined))

/**
 * 生成受限开关的提示行
 *
 * @param blocked 该开关在当前配置下是否不生效(不生效时统一换成说明文案)
 */
function blockedHint(
  blocked: boolean,
  note: string,
  on: boolean,
  onText: string,
  offText: string,
): { text: string; level: HintTone } {
  if (blocked) return { text: note, level: 'warn' }
  return { text: on ? onText : offText, level: on ? 'ok' : 'warn' }
}

// 思考模式：两种模式都支持（旧版直连下线后不再有"不生效"的组合）
const thinkHint = computed<{ text: string; level: HintTone }>(() => ({
  text: settingsStore.thinkEnabled
    ? '已开启：角色先思考再回答（更慢，但更稳）'
    : '已关闭：角色直接回答',
  level: settingsStore.thinkEnabled ? 'ok' : 'warn',
}))
// 【2026-11-18 P0】「强制每条搜索 / 联网搜索 / RAG 语料库」的提示与开关一起删除：
// 新后端 capabilities.web_search=false（只走世界书），这些开关早就名不副实。
/**
 * 「沉浸式对话模式」提示行
 *
 * 两种模式 + 单聊/群聊都已落实(后端过滤或前端本地过滤)→ 永不置灰,
 * 因此不走 blockedHint,只按开关状态给描述。
 */
const immersiveHint = computed<{ text: string; level: HintTone }>(() => {
  const on = settingsStore.immersiveMode
  return {
    level: on ? 'ok' : 'warn',
    text: on
      ? '已开启：角色只说话，不写动作描写'
      : '已关闭：角色会写括号里的动作与神态',
  }
})
const useNewPromptHint = computed(() =>
  blockedHint(
    newPromptBlocked.value,
    NEW_PROMPT_BLOCKED_NOTE,
    settingsStore.useNewPrompt,
    '已开启：使用新版角色设定',
    '已关闭：使用默认角色设定',
  ),
)

/**
 * 「沉浸式对话模式」开启时,两个括号样式开关一起锁住
 *
 * 该模式要求后端**不写**括号动作/神态(只留台词),回复里压根不会出现括号描写,
 * 于是「括号描写居中」「彩色括号描写」都无从生效 —— 置灰并说明原因,
 * 免得玩家拨了开关却看不到任何变化。
 *
 * 与上面几个 blocked 判据的区别:那些是"当前 API 模式不生效"(换模式就恢复),
 * 这个是"上游不会产出可居中的内容"(得先把沉浸式关掉)。
 * 两者互相独立,可能同时成立。
 */
const bracketStyleLocked = computed(() => settingsStore.immersiveMode)

/** 被沉浸式锁住时的统一说明(悬浮提示与提示行共用同一句) */
const IMMERSIVE_LOCK_NOTE = '「沉浸式对话模式」开着时，角色不写括号描写'

/**
 * 「括号描写居中」提示行
 *
 * 三种状态:被沉浸式锁住 → 开关开 / 关。
 * (本项原先在模板里内联三元,加了"锁住"这一态后挪到 computed,避免嵌套太深)
 */
const bracketCenterHint = computed(() => {
  if (bracketStyleLocked.value) {
    return {
      level: 'keep' as const,
      text: '沉浸式模式下没有括号描写，关掉它才能设置这一项',
    }
  }
  return settingsStore.bracketCenter
    ? {
        level: 'ok' as const,
        text: '已开启：括号里的动作与神态单独成行、居中显示',
      }
    : {
        level: 'warn' as const,
        text: '已关闭：括号描写留在气泡里',
      }
})

/**
 * 「彩色括号描写」提示行
 *
 * 两层依赖,按优先级说明:
 *   1. 「沉浸式对话模式」开着 → 上游不产出括号描写,说什么都白搭
 *   2. 「括号描写居中」关着 → 没有居中行,颜色无从谈起
 * 这不是"当前配置下不生效",所以**不用** blockedHint,单独说明依赖关系。
 */
const bracketColorHint = computed(() => {
  if (bracketStyleLocked.value) {
    return {
      level: 'keep' as const,
      text: '沉浸式模式下没有可上色的内容',
    }
  }
  if (!settingsStore.bracketCenter) {
    return {
      level: 'keep' as const,
      text: '需先开启上方「括号描写居中」',
    }
  }
  return settingsStore.bracketColor
    ? {
        level: 'ok' as const,
        text: '已开启：居中描写按角色属性上色（灰 / 红 / 黄 / 青 / 绿 / 紫）',
      }
    : {
        level: 'warn' as const,
        text: '已关闭：所有居中描写统一用半透明白',
      }
})

/**
 * 「居中我方括号内容」提示行
 *
 * 只影响我方消息的开头那对括号;颜色按"我在当前会话里的身份"取属性
 * (单人 = 管理员 physical;群聊里扮演某角色 = 该角色)。
 */
const centerMyBracketHint = computed(() =>
  settingsStore.centerMyBracket
    ? {
        level: 'ok' as const,
        text: '已开启：我方消息开头的括号单独居中，颜色随我的身份',
      }
    : {
        level: 'warn' as const,
        text: '已关闭：我方消息整条一个气泡',
      },
)

// ---- 模型列表(主 API / 总结 API 各一份,逻辑在 useModelPicker 里) -------------
// summaryDraft 必须先于这里的 useModelPicker 声明:composable 内部的 watch
// 在 setup 期间就会读一次 source(),那时若 summaryDraft 还在 TDZ 会直接抛错。
/** 智能总结配置草稿(打开时从 store 同步,保存时写回) */
const summaryDraft = ref({ ...settingsStore.summaryConfig })

/**
 * 智能总结的运行参数（只读展示）
 *
 * 2026-11-14 新架构：上下文窗口按模式决定（后端 131072 / 自定义走用户配置），
 * 触发阈值改由「精细度调整」滑条设置；设置页展示**真实值**：
 * 上次请求占用来自 usage.prompt_tokens（或裁剪后的估算），
 * 本轮保留 K 轮来自当前 contextHistory 的整轮计数。
 */

const {
  options: modelOptions,
  loading: modelListLoading,
  error: modelListError,
  load: loadModels,
} = useModelPicker(() => ({ baseUrl: apiDraft.value.baseUrl, apiKey: apiDraft.value.apiKey }))

/** 服务商在 /models 里给出的上下文长度(键 = 模型 id) */
const apiContextById = computed(() => {
  const map = new Map<string, number>()
  for (const m of modelOptions.value) {
    if (m.contextLength !== undefined) map.set(m.id, m.contextLength)
  }
  return map
})

// ---- 上下文窗口自动获取(models.dev 在线模型库) --------------------------------
/** 在线库的查询结果(尚未查到 / 查不到时为 null) */
const libraryHit = ref<LibraryHit | null>(null)
/** 是否正在查询 */
const libraryFetching = ref(false)

/**
 * 当前模型名对应的上下文窗口推断结果
 *
 * 推断本身是纯函数(utils/modelContext),这里只负责把"外部探到的值"喂进去:
 * 服务商 /models 的精确值、以及 models.dev 在线库的值。
 */
const contextGuess = computed(() => {
  const id = apiDraft.value.model.trim()
  const lib = libraryHit.value
  return guessContextWindow(id, {
    fromApi: apiContextById.value.get(id),
    // 结果必须属于当前模型:异步回来时用户可能已经改了模型名
    fromLibrary: lib && lib.modelId === id.toLowerCase() ? lib.contextWindow : undefined,
  })
})

/**
 * 当前会话的"上次请求占用"（真实 usage.prompt_tokens，裁剪后为估算值）
 *
 * 旧实现展示的是写死的 effectiveBackendPromptTokens()，与真实请求无关；
 * 2026-11-14 起改为读 Conversation.lastPromptTokens（每次请求成功 / 裁剪后写入）。
 */
const summaryLastPromptTokens = computed(() => {
  const sub = chatStore.activeSub
  if (sub === null) return 0
  const conv = chatStore.conversations[sub]
  const value = conv?.lastPromptTokens ?? 0
  return Number.isFinite(value) && value > 0 ? Math.round(value) : 0
})

/** 当前会话请求历史里还有几"整轮"（mine + 其后连续 other） */
const summaryKeptRounds = computed(() => countHistoryRounds(chatStore.getChatHistory()))

/**
 * 自定义 API 模式的上下文窗口（用于展示总结长度）
 *
 * 优先级与运行时一致：apiConfig.contextWindow → 模型库 / 模型名后缀 → 32768。
 * 这里复用设置页已经算好的 contextGuess（含 /models 精确值与 models.dev 在线库）。
 */
const summaryCustomContext = computed(() => {
  if (apiDraft.value.contextWindow > 0) return Math.floor(apiDraft.value.contextWindow)
  const guessed = contextGuess.value.contextWindow
  return guessed > 0 ? guessed : CUSTOM_SUMMARY_FALLBACK_CONTEXT
})

/** 当前模式下判定使用的上下文窗口（旧版直连已下线，这里只剩两种） */
const summaryRuntimeWindow = computed(() => {
  if (apiDraft.value.apiMode === 'custom') return summaryCustomContext.value
  return BACKEND_CONTEXT_WINDOW_TOKENS
})

/** 设置页只读信息行（真实值） */
const summaryRuntimeLine = computed(() => (
  `上次请求占用 ≈ ${formatTokenCount(summaryLastPromptTokens.value)}`
  + ` · 上下文 ≈ ${formatTokenCount(summaryRuntimeWindow.value)}`
  + ` · 触发阈值 ${summaryDraft.value.triggerPercent ?? 80}%`
  + ` · 本轮保留 ${summaryKeptRounds.value} 轮`
))

/** 自定义 API 模式的总结长度说明行 */
const summaryCustomLengthLine = computed(() => (
  formatCustomSummaryInfoLine(summaryCustomContext.value)
))

/**
 * 是否视为"用户从未设置过上下文窗口"
 *
 * 0 = 现在的默认(空);65536 = 旧版默认值,老配置里存的就是它 ——
 * 一起当作"没设置过",否则老用户打开设置时自动获取就不会生效。
 */
function contextWindowUnset(value: number): boolean {
  return !(value > 0) || value === FALLBACK_CONTEXT_WINDOW
}

/**
 * 把获取到的上下文窗口写入草稿(没获取到时保持原值不动,绝不瞎覆盖)
 *
 * @param mode 'change'  用户刚刚换了模型 → 无条件覆盖
 *             'passive' 打开设置 / 模型列表刷新 / 在线库返回 → 只在用户没设置过时才写
 *
 * 为什么必须分这两种:'passive' 若无条件覆盖,用户刻意设的保守值
 * (省 token,或端点实际上限比模型标称更低)每次打开设置都会被改回去,
 * 而且改完用户还不知道是谁改的。'change' 则相反 —— 用户刚换了模型,
 * 旧窗口已经不属于这个模型了,跟着换才对。
 *
 * 【写草稿的同时立刻落库】这个值是"自动获取"来的,界面已经明确告诉用户
 * "自动获取上下文长度成功" —— 若还要再点一次「保存」才生效,忘了点就白获取了。
 * 本字段是该标签页里唯一写穿的:其余字段(baseUrl / key / 温度 …)仍走草稿 + 保存,
 * 因为那些是用户自己敲的,保留"改完再确认"的语义更安全。
 */
function applyContextGuess(mode: 'change' | 'passive' = 'passive'): void {
  const g = contextGuess.value
  if (g.contextWindow <= 0) return
  if (mode === 'passive' && !contextWindowUnset(apiDraft.value.contextWindow)) return
  // 值没变就不必再写一次 store(避免每次打开设置都触发一轮落库)
  if (apiDraft.value.contextWindow === g.contextWindow) return
  apiDraft.value.contextWindow = g.contextWindow
  settingsStore.updateApiConfig({ contextWindow: g.contextWindow })
}

/** 获取到的值与当前填写值不同(不同才显示"应用"按钮) */
const contextGuessApplicable = computed(
  () => contextGuess.value.contextWindow > 0 && contextGuess.value.contextWindow !== apiDraft.value.contextWindow,
)

/**
 * 上下文窗口输入框的双向绑定
 *
 * 单独做一层映射的原因:未设置时用 0 表示,而 number 输入框会把 0 显示成 "0",
 * 看起来像"窗口是 0"。这里把 0 映射成空串,用户清空输入框也存回 0。
 */
const contextWindowInput = computed({
  get: () => (apiDraft.value.contextWindow > 0 ? String(apiDraft.value.contextWindow) : ''),
  set: (raw: string) => {
    const n = Number(String(raw).replace(/[^\d]/g, ''))
    apiDraft.value.contextWindow = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
  },
})

/**
 * 触发压缩占比:用单向 :value + 失焦时夹取回写
 *
 * 不用 v-model 的原因有两个:
 *   1. `.number` 修饰符在输入框为空时会把 `''` 写进 number 字段(类型谎言)
 *   2. 输入过程中不该夹取 —— 否则想输 "100" 时刚敲下 "1" 就被改成 "10"
 * 故只在 change(失焦/回车)时夹到 [10, 100] 并写回 DOM。
 */
function onTriggerPercentChange(event: Event) {
  const el = event.target as HTMLInputElement
  const raw = Number(el.value)
  const clamped = clampTriggerPercent(Number.isFinite(raw) ? raw : DEFAULT_COMPACT_TRIGGER_PERCENT)
  apiDraft.value.compactTriggerPercent = clamped
  el.value = String(clamped)
}

/**
 * 当前设置下的实际触发阈值(Token)
 *
 * 直接调引擎同款的 computeInputBudget,而不是自己算"窗口 × 占比" ——
 * 后者在占比设成 100% 时会给出一个超过窗口的数字,而引擎实际还会被
 * "窗口 − 生成预留 − 安全余量"这道硬上限压住。提示里的数字必须与实际判据一致。
 */
const triggerTokens = computed(() =>
  computeInputBudget(
    apiDraft.value.contextWindow,
    apiDraft.value.maxTokens,
    apiDraft.value.compactTriggerPercent,
  ),
)

/**
 * 查询一次上下文窗口
 *
 * utils/modelsDev 内部有 24 小时内存缓存、single-flight 与失败冷却
 * (解压后 4.5MB,brotli 传输约 330KB),所以"打开设置 / 换模型 / 改 Base URL"
 * 都调一次是安全的 —— 真正打网络的只有本页面加载后的第一次。
 *
 * 查不到时什么都不显示:字段留空,由 placeholder 告诉用户可以手动填。
 */
async function queryContextWindow(): Promise<void> {
  // 只有自定义模式才用得上(其他模式没有 baseUrl/模型可填)
  if (!isCustomMode.value) return
  const model = apiDraft.value.model.trim()
  if (!model) {
    libraryHit.value = null
    return
  }
  libraryFetching.value = true
  const hit = await lookupModelContext(model, apiDraft.value.baseUrl)
  libraryFetching.value = false
  // 结果回来时模型可能已经被改掉,过期结果直接丢弃(否则会给另一个模型套上错的窗口)
  if (apiDraft.value.model.trim() !== model) return
  libraryHit.value = hit
  // 被动应用:只在用户没设置过窗口时才写入
  applyContextGuess()
}

/** 换模型 / 改 Base URL 时重查(轻防抖:连续敲字只查最后一次) */
let contextTimer: number | undefined
watch(
  () => `${apiDraft.value.model}\u0000${apiDraft.value.baseUrl}`,
  () => {
    window.clearTimeout(contextTimer)
    libraryHit.value = null
    if (!apiDraft.value.model.trim()) return
    contextTimer = window.setTimeout(() => void queryContextWindow(), 350)
  },
)

/**
 * 获取模型列表(主 API)
 *
 * @param force true = 忽略缓存(用户手动点击);false = 自动路径,走 utils/modelList 的缓存
 *
 * 列表回来后才可能带出服务商给出的上下文长度,顺势补一次窗口推断
 * (被动模式,不覆盖用户手改过的值)。
 */
async function loadMainModels(force: boolean) {
  await loadModels(force)
  applyContextGuess()
}

/** 手动点击"获取模型列表" */
function onFetchModels() {
  void loadMainModels(true)
}

/**
 * 自动获取(打开弹窗时、切到自定义模式时)
 *
 * 不做"同一组配置只拉一次"的自建去重 —— 去重交给 utils/modelList 的缓存:
 * 成功与失败都缓存 10 分钟,重复调用不会打网络,用户换地址/换 Key 又会自然
 * 变成新的缓存键。多处各写一份去重逻辑反而容易走散。
 */
function maybeAutoFetchModels() {
  if (!isCustomMode.value) return
  if (!apiDraft.value.baseUrl.trim()) return
  void loadMainModels(false)
}

/** 是否为后端模式(地址内置,无需填写) */
// ---- 后端路线(默认 / 备用两个按钮,现在指向**同一个**新后端) ------------------
//
// 渲染位置:「连接设置」页 → 默认模式分支(与它影响的对象在同一处)。
// 自定义模式不经过这条链路,所以不显示。
//
// 【2026-11-18】新后端只有一个部署地址(constants/apiRoutes.ts 的 backendBaseUrl())，
// 这两个按钮现在只是"显示当前打到哪"——点哪个结果都一样，保留是因为
// 排查时能看到实际地址（docs/05 §7 记录了这一点）。
const API_ROUTE_OPTIONS: { value: ApiRoute; label: string; host: string }[] = (
  ['primary', 'backup'] as const
).map((r) => ({
  value: r,
  label: API_ROUTE_LABELS[r],
  host: API_BASES[r].replace(/^https?:\/\//, ''),
}))

/** 当前路线的域名(界面上直接显示出来,方便排查"到底打到哪了") */
const currentRouteHost = computed(
  () => API_ROUTE_OPTIONS.find((o) => o.value === settingsStore.apiRoute)?.host ?? '',
)

const isBackendMode = computed(() => apiDraft.value.apiMode === 'backend')

// 【2026-11-18 P0】isLegacyMode 已删除：旧版直连模式下线，
// 存量用户的 apiMode 在 settings store 启动时已迁移成 'backend'。

/**
 * 启用 / 停用自定义 API
 *
 * 「连接设置」页只保留 默认模式,自定义模式的开关随之挪到
 * 「自定义 API」页 —— "当前用哪套端点"于是只有一个入口,不会两页各说一套。
 * 停用时退回「默认模式」。
 */
function toggleCustomApi() {
  switchApiMode(isCustomMode.value ? 'backend' : 'custom')
}

/** 当前对话的角色名(后端模式连接测试用它解析该角色的固定地址) */
const currentCharName = computed(() =>
  chatStore.activeSub !== null
    ? chatStore.conversations[chatStore.activeSub]?.name ?? ''
    : '',
)

// ---- 提示词编辑 ---------------------------------------------------------
/** 所有可编辑提示词的角色名列表(内置角色) */
const characterNames = computed(() => Object.keys(CHARACTER_PROMPTS))

/** 当前对话的角色名是否在内置表里(用于默认选中) */
const selectedCharacter = computed<string>(() =>
  currentCharName.value && characterNames.value.includes(currentCharName.value)
    ? currentCharName.value
    : '',
)

// ---- 智能总结「精细度调整」（2026-11-14 追加） --------------------------------
/**
 * 边界计算用的提示词 token
 *
 * - 后端模式：写死 4000（提示词在服务端）；
 * - 自定义 API：实测校准值（effectiveBackendPromptTokens 缓存）与当前两条
 *   system 消息（固定提示词 + 世界观 + 角色提示词 + 用户覆盖）估算取较大值。
 * 因此用户改了世界观 / 角色提示词并保存后，这个值会实时变化。
 */
const summaryBoundPromptTokens = computed(() => {
  const name = currentCharName.value || selectedCharacter.value || ''
  return summaryPromptTokens(
    apiDraft.value.apiMode,
    settingsStore.getSystemMessage(),
    name ? settingsStore.getCharacterMessage(name) : '',
  )
})

/** 当前模式下的可调范围（45% ~ 动态上限，最高 80%） */
const summaryTriggerBounds = computed(() => (
  computeSummaryTriggerBounds(summaryBoundPromptTokens.value, summaryRuntimeWindow.value)
))

/** 滑条当前值（始终夹在可调范围内） */
const summaryTriggerCurrent = computed(() => (
  clampSummaryTriggerPercent(summaryDraft.value.triggerPercent, summaryTriggerBounds.value)
))

/** 自动夹取时的提示（几秒后自动消失） */
const summaryTriggerNotice = ref('')
let summaryTriggerNoticeTimer: ReturnType<typeof setTimeout> | undefined
function flashSummaryTriggerNotice(message: string): void {
  summaryTriggerNotice.value = message
  if (summaryTriggerNoticeTimer !== undefined) clearTimeout(summaryTriggerNoticeTimer)
  summaryTriggerNoticeTimer = setTimeout(() => {
    summaryTriggerNoticeTimer = undefined
    summaryTriggerNotice.value = ''
  }, 6000)
}

/**
 * 提示词占比变化 / 模式切换后，把持久化阈值夹回可调范围
 *
 * 需求要求"当前值低于 min 时自动抬到 min 并提示"；同理，当前值高于动态
 * 上限时也必须自动调低，否则"提示词 + 本轮回复"会撑爆窗口。
 */
watch(
  () => [
    summaryTriggerBounds.value.min,
    summaryTriggerBounds.value.max,
    summaryDraft.value.triggerPercent,
  ] as const,
  ([min, max, current]) => {
    if (!Number.isFinite(current)) {
      summaryDraft.value.triggerPercent = max
      settingsStore.updateSummaryConfig({ triggerPercent: max })
      return
    }
    const fixed = clampSummaryTriggerPercent(current, summaryTriggerBounds.value)
    if (fixed === current) return
    summaryDraft.value.triggerPercent = fixed
    // 自动夹取发生在 summaryDraft 持久化 watch 注册之前（setup 期 immediate），
    // 这里直接写 store，保证"打开设置就被调整"的值也能落库。
    settingsStore.updateSummaryConfig({ triggerPercent: fixed })
    if (fixed > current) {
      flashSummaryTriggerNotice(`触发阈值已从 ${current}% 抬到 ${fixed}%（不能低于 ${min}% 的下限）`)
    } else {
      flashSummaryTriggerNotice(
        `提示词占用变化，触发阈值已从 ${current}% 调到 ${fixed}%（给本轮回复留足空间）`,
      )
    }
  },
  { immediate: true },
)

/** 滑条输入：实时写进草稿（deep watch 负责落库），并夹在范围内 */
function onSummaryTriggerInput(event: Event): void {
  const el = event.target as HTMLInputElement
  const n = Number(el.value)
  summaryDraft.value.triggerPercent = clampSummaryTriggerPercent(
    Number.isFinite(n) ? n : summaryTriggerBounds.value.max,
    summaryTriggerBounds.value,
  )
}

/** 只读信息行：上下文 / 提示词 / 可调范围 / 当前值 */
const summaryPrecisionLine = computed(() => {
  const b = summaryTriggerBounds.value
  return `上下文 ${formatTokenCount(b.contextWindow)}`
    + ` · 提示词 ≈${formatTokenCount(b.promptTokens)}（${b.promptPercent.toFixed(1)}%）`
    + ` · 可调范围 ${b.min}%–${b.max}% · 当前 ${summaryTriggerCurrent.value}%`
})

/** 当前百分比折算成 token 的触发点 */
const summaryTriggerTokenLine = computed(() => (
  `≈${formatTokenCount(triggerTokenEstimate(
    summaryTriggerBounds.value.contextWindow,
    summaryTriggerCurrent.value,
  ))} token 时触发`
))

/**
 * 「提示词」页当前编辑的角色
 *
 * 与旧实现不同:这里**不跟随当前对话**,而是由用户在下拉里自己选 ——
 * 想改谁就选谁,不必先切对话。
 */
const promptChar = ref<string>('')

/** 提示词草稿(对应 promptChar 的角色) */
const characterPromptDraft = ref('')

/** 只有自定义 API 模式下提示词才生效,所以也只有那时可编辑 */
const promptsEditable = computed(() => isCustomMode.value)

/** 当前角色是否已经用了自定义提示词(决定「恢复默认」是否出现) */
const promptOverridden = computed(
  () => !!promptChar.value && promptChar.value in settingsStore.promptOverrides,
)

/** 把 promptChar 对应的提示词读进草稿 */
function loadPromptDraft(): void {
  characterPromptDraft.value = promptChar.value
    ? settingsStore.getCharacterPrompt(promptChar.value)
    : ''
}

// 切换角色时同步草稿
watch(promptChar, () => loadPromptDraft())

// 打开弹窗时同步本地缓存
watch(
  () => props.open,
  (open) => {
    if (open) {
      apiDraft.value = { ...settingsStore.apiConfig }
      // 每次打开面板先清掉上一次的领取结果/错误：它们属于"刚才那次操作"，
      // 隔了一轮再打开还挂着会让人以为刚刚才发生过
      claimSummary.value = ''
      claimError.value = ''
      accountClaimError.value = ''
      // 智能总结草稿(打开时从 store 同步)
      summaryDraft.value = { ...settingsStore.summaryConfig }
      // 提示词页:默认选中当前对话的角色,没有就取第一个
      if (!promptChar.value || !characterNames.value.includes(promptChar.value)) {
        promptChar.value = selectedCharacter.value || characterNames.value[0] || ''
      }
      loadPromptDraft()
      // 自动填上下文窗口(被动模式:仅当用户没设置过,绝不覆盖手改过的值)
      applyContextGuess()
      // 再查一次在线模型库(内部有 24h 缓存与失败冷却,重复打开不会重复下载)
      void queryContextWindow()
      // 自定义模式下自动拉一次模型列表(已缓存则瞬时返回,不打网络)
      maybeAutoFetchModels()
      // 账号状态还没问过(启动时网络失败 / 首次打开)：打开面板顺手重试一次
      if (accountStore.state === 'unknown' && !accountStore.busy) {
        void accountStore.refresh(true)
      }
      // 列表数据源：顺手重读一次迁移标记 —— fe-migrate 可能刚迁完，
      // 不重读的话「列表数据源」那一行还显示"先迁移"（P1）
      listStore.refreshMigratedFlag()
    }
  },
)

/** 切到「账号」页时，如果状态还是未知（上次拉取失败）就再问一次 */
watch(activeTab, (tab) => {
  if (tab === 'account' && accountStore.state === 'unknown' && !accountStore.busy) {
    void accountStore.refresh(true)
  }
})

/**
 * 智能总结开关的提示行
 *
 * 两种情况：
 *   - 默认模式：由服务端自动压缩/总结，前端开关不参与（文案必须说清楚，见 docs/08）；
 *   - 自定义 API：前端直连用户端点，45~80% 动态阈值仍有效。
 */
const summaryHint = computed(() => {
  if (summaryServerManaged.value) {
    return {
      level: 'keep' as const,
      text: '默认模式由服务端自动压缩/总结（约 80% 触发、压到 50%），这里不用调；该开关只对「自定义 API」生效',
    }
  }
  return blockedHint(
    summaryBlocked.value,
    SUMMARY_BLOCKED_NOTE,
    summaryDraft.value.enabled,
    '已开启：对话快满时自动把早期内容总结成摘要',
    '已关闭：不自动总结早期记忆',
  )
})

/**
 * 智能总结配置草稿 → store 的即时写穿
 *
 * 与 API 配置同一套做法:开关、总结 API 模式、自定义地址/密钥/模型,改一处即落库,
 * 不再需要点「保存总结设置」—— 那个按钮已随之移除。
 */
watch(
  summaryDraft,
  (draft) => {
    settingsStore.updateSummaryConfig({ ...draft })
  },
  { deep: true },
)

/**
 * API 配置草稿 → store 的即时写穿
 *
 * 自定义 API 模式下的这些配置(Base URL / 密钥 / 模型名 / 温度 / 最大 Token /
 * 上下文窗口 / 触发占比 / 自动压缩 / 流式用量 …)一律**改一处即落库**,
 * 不再需要点底部的「保存」—— 那个按钮已随之移除,免得留一个点了没意义的按钮。
 *
 * 为什么保留草稿层而不是把输入框直接双向绑定到 store:
 *   打开弹窗时要先把 store 快照一次性灌进草稿(见上面的 watch(props.open)),
 *   写穿则负责"编辑 → store"这一个方向。两者分开,各自只做一件事。
 *
 * 成本:每次按键写一次 localStorage,载荷只有几百字节(与 worldView 的现有写法
 * 一致);IndexedDB 那一侧还有 300ms 防抖兜底,不会每次击键都落盘。
 */
watch(
  apiDraft,
  (draft) => {
    settingsStore.updateApiConfig({ ...draft })
  },
  { deep: true },
)

/**
 * 切换 API 模式(默认 API / 自定义 API):立即持久化,无需点保存。
 * 切换时同步草稿与 store,关闭弹窗再打开仍是新模式。
 */
function switchApiMode(mode: 'custom' | 'backend') {
  apiDraft.value.apiMode = mode
  // 模式以外的字段由上面的写穿 watch 一并同步,这里只需保证模式立即生效
  maybeAutoFetchModels()
}

/** 连接测试状态 */
const testState = ref<'idle' | 'testing' | 'success' | 'fail'>('idle')
const testMessage = ref('')

/** 测试 API 连接 */
async function onTestConnection() {
  testState.value = 'testing'
  testMessage.value = ''
  try {
    const result = isBackendMode.value
      ? await testBackendConnection({ ...apiDraft.value }, currentCharName.value)
      : await testApiConnection({ ...apiDraft.value })
    testState.value = result.ok ? 'success' : 'fail'
    testMessage.value = result.message
  } catch {
    testState.value = 'fail'
    testMessage.value = '连接失败: 未知错误'
  }
}

/** 保存角色提示词(保存到「提示词」页当前选中的角色) */
function saveCharacterPrompt() {
  if (!promptChar.value) return
  settingsStore.setPromptOverride(promptChar.value, characterPromptDraft.value)
}

/** 重置角色提示词为内置默认 */
function resetCharacterPrompt() {
  if (!promptChar.value) return
  settingsStore.resetPromptOverride(promptChar.value)
  loadPromptDraft()
}

/** 重置全部设置 */
function resetAll() {
  settingsStore.resetAll()
  apiDraft.value = { ...settingsStore.apiConfig }
  loadPromptDraft()
}

</script>

<template>
  <Transition name="pn-fade">
    <div v-if="open" class="st">
      <div class="pn-scrim" aria-hidden="true"></div>

      <!-- 全屏面板:顶部分类页签 + 内容滚动区 -->
      <div class="pn-win st__win" role="dialog" aria-modal="true" :aria-label="`设置 · ${activeLabel}`">
        <div class="st__top">
          <!-- 左上角:与主界面同款的小图 + 标题 -->
          <div class="st__brand" aria-hidden="true">
            <img class="st__brand-deco" :src="MATERIALS.headerDeco" alt="" />
            <p class="st__brand-title">//设置</p>
          </div>

          <!-- 分类页签 -->
          <nav class="pn-tabs" role="tablist" aria-label="设置分类">
            <span class="pn-tabs__backdrop" aria-hidden="true"></span>
            <button
              v-for="t in SETTINGS_NAV"
              :key="t.key"
              class="pn-tab"
              :class="{ 'is-active': activeTab === t.key }"
              type="button"
              role="tab"
              :aria-selected="activeTab === t.key"
              :title="t.label"
              @click="onSelectTab(t.key)"
            >
              <span
                class="pn-tab__block"
                aria-hidden="true"
                :style="activeTab === t.key ? { backgroundImage: `url(${MATERIALS.panelTabTexture})` } : undefined"
              ></span>
              <span class="pn-tab__icon">
                <svg viewBox="0 0 48 48" aria-hidden="true">
                  <path
                    v-for="(p, i) in t.icon"
                    :key="i"
                    :d="p.d"
                    :fill="p.filled ? 'currentColor' : 'none'"
                    :stroke="p.filled ? 'none' : 'currentColor'"
                    stroke-width="3.4"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                </svg>
              </span>
              <span class="pn-tab__label">{{ t.short }}</span>
            </button>
          </nav>

          <button class="pn-close" type="button" aria-label="关闭设置" @click="emit('close')">
            <svg viewBox="0 0 205 205" fill="currentColor" fill-rule="evenodd" aria-hidden="true">
              <path d="M0 25L25 0L205 180L180 205ZM25 205L0 180L180 0L205 25Z"></path>
            </svg>
          </button>
        </div>

        <!-- ==================== 内容区 ==================== -->
        <div ref="bodyEl" class="pn-body st__body">
          <!-- ============ 1. API 配置 ============ -->
          <!-- 账号（登录 / 注册 / 退出 / 改密 / 设备） -->
          <template v-if="activeTab === 'account'">
            <SetSection title="身份">
              <SetRow
                label="当前身份"
                desc="不登录也能聊天：记录只保存在这台设备上"
              >
                <span class="st__ro">
                  {{ accountStore.state === 'unknown'
                    ? '正在连接服务器…'
                    : accountStore.state === 'user'
                      ? (accountStore.user?.display_name || accountStore.user?.username || '已登录')
                      : '访客' }}
                </span>
              </SetRow>

              <SetRow
                v-if="accountStore.state === 'user'"
                label="用户名"
                desc="登录后，这台设备上的记录会归到账号名下"
              >
                <span class="st__ro">@{{ accountStore.user?.username }}</span>
              </SetRow>

              <SetRow
                v-if="accountStore.state === 'user'"
                label="名下设备"
                :desc="`共 ${accountStore.devices.length} 台（同一账号登录过的设备都算）`"
                stack
              >
                <ul class="st__devices">
                  <li v-for="d in accountStore.devices" :key="d.id">
                    <span>
                      {{ platformLabel(d.platform) }}
                      <template v-if="d.app_ver"> · {{ d.app_ver }}</template>
                    </span>
                    <span class="st__devices-time">最近 {{ shortTime(d.last_seen_at) }}</span>
                  </li>
                </ul>
              </SetRow>

              <SetRow
                v-if="accountStore.state === 'user'"
                label="账号操作"
                desc="登录只是把设备挂到账号上；token 不换、聊天不用重连"
                stack
              >
                <div class="st__btnrow">
                  <button
                    class="pn-pill"
                    type="button"
                    :disabled="accountStore.busy"
                    @click="accountStore.refresh()"
                  >刷新</button>
                  <button
                    class="pn-pill st__danger"
                    type="button"
                    :disabled="accountStore.busy"
                    @click="onAccountLogout"
                  >退出登录</button>
                </div>
              </SetRow>
            </SetSection>

            <!-- 访客：登录 / 注册 -->
            <SetSection v-if="accountStore.state !== 'user'" title="登录 / 注册">
              <SetRow label="登录方式" desc="用户名 + 密码；登录后记录归账号，换设备也能看见">
                <SegControl
                  :model-value="accountMode"
                  :options="ACCOUNT_MODE_SEG"
                  aria-label="登录方式"
                  @update:model-value="switchAccountMode"
                />
              </SetRow>

              <SetRow
                label="用户名"
                :hint="accountMode === 'register' ? accountUsernameHint : ''"
                tone="keep"
                stack
              >
                <div class="st__stack">
                  <input
                    v-model="accountUsername"
                    class="pn-input"
                    type="text"
                    autocomplete="username"
                    :placeholder="accountMode === 'register' ? '2–20 字，中文/字母/数字/下划线' : '用户名'"
                    :disabled="accountStore.busy"
                  />
                </div>
              </SetRow>

              <SetRow label="密码" :hint="accountPasswordHint" tone="keep" stack>
                <div class="st__stack">
                  <input
                    v-model="accountPassword"
                    class="pn-input"
                    type="password"
                    :autocomplete="accountMode === 'register' ? 'new-password' : 'current-password'"
                    :disabled="accountStore.busy"
                  />
                </div>
              </SetRow>

              <SetRow v-if="accountMode === 'register'" label="确认密码" stack>
                <div class="st__stack">
                  <input
                    v-model="accountConfirm"
                    class="pn-input"
                    type="password"
                    autocomplete="new-password"
                    :disabled="accountStore.busy"
                    @keyup.enter="onAccountSubmit"
                  />
                </div>
              </SetRow>

              <SetRow v-if="accountMode === 'register'" label="昵称（可选）" desc="不填就用用户名" stack>
                <div class="st__stack">
                  <input
                    v-model="accountDisplayName"
                    class="pn-input"
                    type="text"
                    :disabled="accountStore.busy"
                    @keyup.enter="onAccountSubmit"
                  />
                </div>
              </SetRow>

              <!--
                取件码（可选）：在别的设备「数据管理 → 一键上传数据」会得到一个 8 位码，
                填在这里注册，后端会顺手把那份包领到这个新账号名下（字段 claim_code）。
              -->
              <SetRow
                v-if="accountMode === 'register'"
                label="取件码（可选）"
                desc="在别的设备上传数据拿到的 8 位码：填了就在注册时直接把那份数据领到这个账号"
                stack
              >
                <div class="st__stack">
                  <input
                    :value="accountClaimCode"
                    class="pn-input st__code-input"
                    :class="{ 'st__code-input--invalid': !!accountClaimError }"
                    type="text"
                    inputmode="text"
                    autocapitalize="characters"
                    autocomplete="off"
                    spellcheck="false"
                    :maxlength="CLAIM_CODE_LEN + 4"
                    placeholder="8 位取件码（不填也能注册）"
                    :disabled="accountStore.busy"
                    @input="(e) => onClaimCodeInput(e, 'register')"
                    @keyup.enter="onAccountSubmit"
                  />
                  <!-- 醒目红底：这条说明"这次注册整个没成功" -->
                  <p v-if="accountClaimError" class="st__claim-error">{{ accountClaimError }}</p>
                </div>
              </SetRow>

              <SetRow label=" " stack>
                <div class="st__btnrow">
                  <button
                    class="pn-pill pn-pill--primary"
                    type="button"
                    :disabled="accountStore.busy"
                    @click="onAccountSubmit"
                  >{{ accountStore.busy ? '处理中…' : (accountMode === 'register' ? '注册并登录' : '登录') }}</button>
                </div>
              </SetRow>
            </SetSection>

            <!-- 已登录：用取件码领取历史数据（与「数据管理 → 拉取历史数据」同一套接口） -->
            <SetSection v-if="accountStore.state === 'user'" title="领取历史数据">
              <SetRow
                label="取件码"
                desc="在别的设备「数据管理 → 一键上传数据」拿到的 8 位码，24 小时内有效、可重复领取"
                stack
              >
                <div class="st__stack">
                  <input
                    :value="claimEntryCode"
                    class="pn-input st__code-input"
                    type="text"
                    inputmode="text"
                    autocapitalize="characters"
                    autocomplete="off"
                    spellcheck="false"
                    :maxlength="CLAIM_CODE_LEN + 4"
                    placeholder="输入 8 位取件码"
                    :disabled="claimBusy || accountStore.busy"
                    @input="(e) => onClaimCodeInput(e, 'entry')"
                    @keyup.enter="onClaimSubmit"
                  />
                  <div class="st__btnrow">
                    <button
                      class="pn-pill pn-pill--primary"
                      type="button"
                      :disabled="claimBusy || accountStore.busy"
                      @click="onClaimSubmit"
                    >{{ claimBusy ? '领取中…' : '验证并领取' }}</button>
                  </div>
                  <p v-if="claimError" class="st__claim-error">{{ claimError }}</p>
                </div>
              </SetRow>
            </SetSection>

            <!-- 已登录：改密码 -->
            <SetSection v-if="accountStore.state === 'user'" title="修改密码">
              <SetRow label="旧密码" stack>
                <div class="st__stack">
                  <input
                    v-model="accountOldPassword"
                    class="pn-input"
                    type="password"
                    autocomplete="current-password"
                    :disabled="accountStore.busy"
                  />
                </div>
              </SetRow>
              <SetRow label="新密码" :hint="accountPasswordHint" tone="keep" stack>
                <div class="st__stack">
                  <input
                    v-model="accountNewPassword"
                    class="pn-input"
                    type="password"
                    autocomplete="new-password"
                    :disabled="accountStore.busy"
                  />
                </div>
              </SetRow>
              <SetRow label="确认新密码" stack>
                <div class="st__stack">
                  <input
                    v-model="accountNewConfirm"
                    class="pn-input"
                    type="password"
                    autocomplete="new-password"
                    :disabled="accountStore.busy"
                    @keyup.enter="onChangePassword"
                  />
                </div>
              </SetRow>
              <SetRow
                label=" "
                desc="改完后其它设备会被登出；这台不用重新登录"
                stack
              >
                <div class="st__btnrow">
                  <button
                    class="pn-pill pn-pill--primary"
                    type="button"
                    :disabled="accountStore.busy"
                    @click="onChangePassword"
                  >修改密码</button>
                </div>
              </SetRow>
            </SetSection>

            <p v-if="accountFormError" class="pn-hint pn-hint--warn">{{ accountFormError }}</p>
            <p
              v-if="accountStore.notice"
              class="pn-hint"
              :class="accountStore.noticeIsError ? 'pn-hint--warn' : 'pn-hint--ok'"
            >{{ accountStore.notice }}</p>
            <!-- 领取结果摘要：放在两段之外，注册成功后页面切成"已登录"也不会被吞掉 -->
            <p v-if="claimSummary" class="st__claim-ok">{{ claimSummary }}</p>
          </template>

          <template v-if="activeTab === 'connection'">
            <!-- 存量用户提示：以前存的「旧版模式」已自动切回默认模式 -->
            <SetSection v-if="settingsStore.legacyRetiredNotice" title="模式已更新">
              <SetRow
                label="旧版直连模式已下线"
                desc="它依赖的取 Key 接口新后端已不再提供；已自动切回「默认模式」，聊天记录不受影响。"
                tone="warn"
                stack
              >
                <button
                  type="button"
                  class="pn-btn pn-btn--light"
                  @click="settingsStore.dismissLegacyRetiredNotice()"
                >
                  <span class="pn-btn__label">知道了</span>
                </button>
              </SetRow>
            </SetSection>

            <SetSection title="接口模式">
              <SetRow label="API 模式" desc="选择聊天使用哪套接口">
                <SegControl
                  :model-value="apiDraft.apiMode"
                  :options="[
                    { value: 'backend', label: '默认模式' },
                    { value: 'custom', label: '自定义API' },
                  ]"
                  aria-label="API 模式"
                  @update:model-value="onApiModeChange"
                />
              </SetRow>

              <!-- 默认 API:后端地址由服务端下发,这里只选线路 -->
              <SetRow
                v-if="isBackendMode"
                label="当前干员"
                desc="默认接口免费提供，提示词由官方维护"
              >
                <span class="st__ro">{{ currentCharName || '未选中' }}</span>
              </SetRow>

              <SetRow
                v-if="isBackendMode"
                label="后端路线"
                :hint="`当前：${currentRouteHost}　默认线路不通时可切到「备用」`"
                tone="ok"
                stack
              >
                <div class="st__stack">
                  <SegControl
                    :model-value="settingsStore.apiRoute"
                    :options="API_ROUTE_OPTIONS.map(o => ({ value: o.value, label: o.label }))"
                    aria-label="后端路线"
                    auto
                    @update:model-value="onApiRouteChange"
                  />
                </div>
              </SetRow>

              <!-- 数据存储（2026-11-20 本地优先：本机永远有一份，云端只是备份/补齐） -->
              <SetRow
                label="数据存储"
                :hint="listSourceHint.text"
                :tone="listSourceHint.level"
                stack
              >
                <div class="st__stack">
                  <SegControl
                    :model-value="listStore.requestedSource"
                    :options="[
                      { value: 'local', label: '本机（默认）' },
                      { value: 'server', label: '本机 + 云端' },
                    ]"
                    aria-label="数据存储"
                    auto
                    @update:model-value="onListSourceChange"
                  />
                </div>
              </SetRow>
            </SetSection>

            <template v-if="isCustomMode">
              <SetSection title="自定义接口">
                <SetRow label="Base URL" stack>
                  <div class="st__stack">
                    <input
                      v-model="apiDraft.baseUrl"
                      class="pn-input"
                      type="text"
                      placeholder="https://api.openai.com/v1"
                    />
                  </div>
                </SetRow>

                <SetRow label="API Key" stack>
                  <div class="st__stack">
                    <input
                      v-model="apiDraft.apiKey"
                      class="pn-input"
                      type="password"
                      placeholder="sk-..."
                      autocomplete="off"
                    />
                  </div>
                </SetRow>

                <SetRow label="模型名" stack>
                  <div class="st__stack">
                    <input
                      v-model="apiDraft.model"
                      class="pn-input"
                      type="text"
                      placeholder="deepseek-v4-flash / glm-5.2 / kimi-k3 / ..."
                      @change="applyContextGuess('change')"
                    />
                  </div>
                </SetRow>

                <!-- 从 Base URL 的 /models 拉取,下拉直接选(免手敲模型名) -->
                <SetRow
                  label="模型列表"
                  :hint="modelListError
                    ? modelListError
                    : (modelOptions.length ? `已获取 ${modelOptions.length} 个，可在下方选择` : '')"
                  :tone="modelListError ? 'warn' : 'ok'"
                  stack
                >
                  <div class="st__stack">
                    <div class="st__btnrow">
                      <button
                        class="pn-pill"
                        type="button"
                        :disabled="modelListLoading"
                        @click="onFetchModels"
                      >{{ modelListLoading ? '获取中…' : '获取模型列表' }}</button>
                    </div>
                    <select
                      v-if="modelOptions.length"
                      v-model="apiDraft.model"
                      class="pn-select"
                      @change="applyContextGuess('change')"
                    >
                      <option value="">— 从列表选择 —</option>
                      <option v-for="m in modelOptions" :key="m.id" :value="m.id">{{ m.id }}</option>
                    </select>
                  </div>
                </SetRow>
              </SetSection>

              <SetSection title="生成参数">
                <SetRow label="温度" :desc="`当前 ${apiDraft.temperature.toFixed(1)}，越高越发散`" stack>
                  <div class="st__stack">
                    <input
                      v-model.number="apiDraft.temperature"
                      class="st__range"
                      type="range"
                      min="0"
                      max="2"
                      step="0.1"
                    />
                  </div>
                </SetRow>

                <SetRow label="最大 Token 数" stack>
                  <div class="st__stack">
                    <input
                      v-model.number="apiDraft.maxTokens"
                      class="pn-input"
                      type="number"
                      min="1"
                      max="32768"
                    />
                  </div>
                </SetRow>

                <!-- 上下文窗口:输入预算与"上下文已用"百分比的分母,无法从 API 探测 -->
                <SetRow
                  label="上下文窗口 (Token)"
                  :hint="contextGuess.contextWindow > 0
                    ? `已自动识别：${contextGuess.contextWindow}`
                    : (libraryFetching ? '正在识别模型上下文…' : '留空会自动识别，识别不到再手动填')"
                  :tone="contextGuess.contextWindow > 0 ? 'ok' : 'keep'"
                  stack
                >
                  <div class="st__stack">
                    <!-- 不能挂 @change="applyContextGuess('change')" ——
                         那个"无条件覆盖"是给"模型名变了"用的,挂在这里会让手改的值被填回去 -->
                    <input
                      v-model="contextWindowInput"
                      class="pn-input"
                      type="number"
                      min="1024"
                      step="1024"
                      placeholder="留空会自动识别，识别不到再手动填"
                    />
                    <div v-if="contextGuessApplicable" class="st__btnrow">
                      <button class="pn-pill" type="button" @click="applyContextGuess('change')">
                        应用 {{ contextGuess.contextWindow }}
                      </button>
                    </div>
                  </div>
                </SetRow>

                <SetRow
                  label="触发压缩的上下文占比 (%)"
                  :hint="`约 ${triggerTokens.toLocaleString('en-US')} Token 时开始压缩（占窗口 ${apiDraft.compactTriggerPercent}%）`"
                  stack
                >
                  <div class="st__stack">
                    <input
                      :value="apiDraft.compactTriggerPercent"
                      class="pn-input"
                      type="number"
                      :min="MIN_COMPACT_TRIGGER_PERCENT"
                      :max="MAX_COMPACT_TRIGGER_PERCENT"
                      step="5"
                      @change="onTriggerPercentChange"
                    />
                  </div>
                </SetRow>

                <SetRow
                  label="自动压缩上下文"
                  :hint="apiDraft.autoCompact
                    ? '已开启：快满时先把早期对话总结成摘要，再丢掉原文'
                    : '已关闭：快满时直接丢掉最早的对话'"
                  :tone="apiDraft.autoCompact ? 'ok' : 'warn'"
                >
                  <SegControl
                    :model-value="apiDraft.autoCompact ? 'on' : 'off'"
                    :options="BOOL_SEG"
                    aria-label="自动压缩上下文"
                    @update:model-value="(v) => (apiDraft.autoCompact = v === 'on')"
                  />
                </SetRow>

                <SetRow
                  label="请求流式用量 (usage)"
                  :hint="apiDraft.requestUsage
                    ? '已开启：能读到真实的输入 / 输出 / 缓存用量'
                    : '已关闭：用量面板只显示估算值'"
                  :tone="apiDraft.requestUsage ? 'ok' : 'warn'"
                >
                  <SegControl
                    :model-value="apiDraft.requestUsage ? 'on' : 'off'"
                    :options="BOOL_SEG"
                    aria-label="请求流式用量"
                    @update:model-value="(v) => (apiDraft.requestUsage = v === 'on')"
                  />
                </SetRow>
              </SetSection>
            </template>

            <SetSection title="连接">
              <SetRow
                label="连接测试"
                :desc="settingsStore.isApiConfigured ? 'API 已配置' : 'API 未配置'"
                :hint="testState === 'success' || testState === 'fail' ? testMessage : ''"
                :tone="testState === 'success' ? 'ok' : (testState === 'fail' ? 'warn' : '')"
                stack
              >
                <div class="st__stack">
                  <div class="st__btnrow">
                    <button
                      class="pn-pill"
                      type="button"
                      :disabled="testState === 'testing'"
                      @click="onTestConnection"
                    >{{ testState === 'testing' ? '测试中…' : '连接测试' }}</button>
                    <button class="pn-pill st__danger" type="button" @click="resetAll">重置全部</button>
                  </div>
                </div>
              </SetRow>
            </SetSection>
          </template>

          <!-- ============ 2. 提示词 ============ -->
          <template v-if="activeTab === 'prompts'">
            <!-- 非自定义 API 模式:提示词不参与请求,整页置为只读说明 -->
            <SetSection v-if="!promptsEditable" title="当前模式不可编辑">
              <SetRow
                label="提示词只在「自定义 API」模式下生效"
                desc="默认模式的提示词在服务端，不能在这里改；切到「自定义 API」后就能逐个角色修改"
              >
                <span class="st__ro">只读</span>
              </SetRow>
            </SetSection>

            <SetSection title="选择角色">
              <SetRow
                label="编辑对象"
                :hint="`带「已修改」的角色用的是你写的版本（共 ${Object.keys(settingsStore.promptOverrides).length} 个）`"
                tone="keep"
              >
                <select
                  v-model="promptChar"
                  class="pn-select"
                  :disabled="!promptsEditable"
                  aria-label="选择要修改提示词的角色"
                >
                  <option v-for="name in characterNames" :key="name" :value="name">
                    {{ name }}{{ name in settingsStore.promptOverrides ? '（已修改）' : '' }}
                  </option>
                </select>
              </SetRow>
            </SetSection>

            <SetSection title="角色提示词">
              <SetRow
                :label="`${promptChar || '未选择角色'} 的提示词`"
                :desc="promptOverridden ? '正在使用你写的版本，可随时恢复默认' : '当前是内置版本，保存后改用你的版本'"
                stack
              >
                <div class="st__stack">
                  <textarea
                    v-model="characterPromptDraft"
                    class="pn-area"
                    rows="14"
                    :placeholder="promptsEditable ? '输入这个角色的提示词…' : '切换到自定义 API 模式后可编辑'"
                    :disabled="!promptsEditable || !promptChar"
                  ></textarea>
                  <div class="st__btnrow">
                    <button
                      class="pn-pill pn-pill--primary"
                      type="button"
                      :disabled="!promptsEditable || !promptChar"
                      @click="saveCharacterPrompt"
                    >保存</button>
                    <button
                      v-if="promptOverridden"
                      class="pn-pill pn-pill--ghost"
                      type="button"
                      :disabled="!promptsEditable"
                      @click="resetCharacterPrompt"
                    >恢复默认</button>
                  </div>
                </div>
              </SetRow>
            </SetSection>

            <SetSection title="世界观背景">
              <SetRow
                label="全局世界观"
                desc="会附加给所有角色，影响整体氛围与设定"
                stack
              >
                <div class="st__stack">
                  <textarea
                    v-model="settingsStore.worldView"
                    class="pn-area"
                    rows="8"
                    placeholder="输入全局世界观，例如：这是终末地工业时代，源石技艺与科技并存…"
                    :disabled="!promptsEditable"
                  ></textarea>
                </div>
              </SetRow>
            </SetSection>
          </template>

          <!-- ============ 3. 显示和内容输出 ============ -->
          <template v-if="activeTab === 'display'">
            <SetSection title="消息分条">
              <SetRow
                label="智能消息分条"
                :hint="settingsStore.smartSplit
                  ? '已开启：动作描写和每句台词各自成行；太短的句子会并到下一行'
                  : '已关闭：只按换行分行，动作描写留在原段落里'"
                :tone="settingsStore.smartSplit ? 'ok' : 'warn'"
              >
                <SegControl
                  :model-value="settingsStore.smartSplit ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="智能消息分条"
                  @update:model-value="(v) => (settingsStore.smartSplit = v === 'on')"
                />
              </SetRow>
            </SetSection>

            <SetSection title="括号描写">
              <SetRow
                label="括号描写居中"
                :hint="bracketCenterHint.text"
                :tone="bracketCenterHint.level"
              >
                <SegControl
                  :model-value="settingsStore.bracketCenter ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  :disabled="bracketStyleLocked"
                  :title="bracketStyleLocked ? IMMERSIVE_LOCK_NOTE : ''"
                  aria-label="括号描写居中"
                  @update:model-value="(v) => (settingsStore.bracketCenter = v === 'on')"
                />
              </SetRow>

              <SetRow
                label="彩色括号描写"
                :hint="bracketColorHint.text"
                :tone="bracketColorHint.level"
              >
                <SegControl
                  :model-value="settingsStore.bracketColor ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  :disabled="!settingsStore.bracketCenter || bracketStyleLocked"
                  :title="bracketStyleLocked
                    ? IMMERSIVE_LOCK_NOTE
                    : (settingsStore.bracketCenter ? '' : '需先开启上方「括号描写居中」')"
                  aria-label="彩色括号描写"
                  @update:model-value="(v) => (settingsStore.bracketColor = v === 'on')"
                />
              </SetRow>

              <SetRow
                label="居中我方括号内容"
                :hint="centerMyBracketHint.text"
                :tone="centerMyBracketHint.level"
              >
                <SegControl
                  :model-value="settingsStore.centerMyBracket ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="居中我方括号内容"
                  @update:model-value="(v) => (settingsStore.centerMyBracket = v === 'on')"
                />
              </SetRow>
            </SetSection>

            <SetSection title="输入面板">
              <SetRow
                label="新版输入面板"
                :hint="settingsStore.newInputPanel
                  ? '已开启：输入框超过两行会出现放大按钮，可展开成大半屏（手机端）'
                  : '已关闭：输入框固定两行高，超出部分在框内滚动'"
                :tone="settingsStore.newInputPanel ? 'ok' : 'warn'"
              >
                <SegControl
                  :model-value="settingsStore.newInputPanel ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="新版输入面板"
                  @update:model-value="(v) => (settingsStore.newInputPanel = v === 'on')"
                />
              </SetRow>

              <SetRow
                label="推荐回复"
                :hint="settingsStore.choicesEnabled
                  ? '已开启：输入框旁显示推荐回复按钮'
                  : '已关闭：不显示推荐回复按钮'"
                :tone="settingsStore.choicesEnabled ? 'ok' : 'warn'"
              >
                <SegControl
                  :model-value="settingsStore.choicesEnabled ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="推荐回复"
                  @update:model-value="(v) => (settingsStore.choicesEnabled = v === 'on')"
                />
              </SetRow>
            </SetSection>

            <SetSection title="回复行为">
              <SetRow
                label="SSE 流式回复"
                :hint="settingsStore.sseStreaming
                  ? '已开启：单聊里回复逐字出现（仅默认 API 生效，不支持时会自动改回整段显示）'
                  : '已关闭：等整段回复生成完再显示'"
                :tone="settingsStore.sseStreaming ? 'ok' : 'warn'"
              >
                <SegControl
                  :model-value="settingsStore.sseStreaming ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="SSE 流式回复"
                  @update:model-value="(v) => (settingsStore.sseStreaming = v === 'on')"
                />
              </SetRow>

              <SetRow
                label="沉浸式对话模式"
                :hint="immersiveHint.text"
                :tone="immersiveHint.level"
              >
                <SegControl
                  :model-value="settingsStore.immersiveMode ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="沉浸式对话模式"
                  @update:model-value="(v) => (settingsStore.immersiveMode = v === 'on')"
                />
              </SetRow>
            </SetSection>

            <SetSection title="时间感知">
              <!-- 与「显示和内容输出」同一页：它影响的是"角色怎么说话"，不是账号/接口 -->
              <SetRow
                label="时间感知"
                :hint="timeAware
                  ? '已开启：她按你设备的真实时间说话（凌晨三点不会说成下午三点）'
                  : '已关闭：她不知道现在几点，也不会说「这么晚还不睡」这类话'"
                :tone="timeAware ? 'ok' : 'warn'"
              >
                <SegControl
                  :model-value="timeAware ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="时间感知"
                  @update:model-value="onTimeAwareChange"
                />
              </SetRow>
              <SetRow
                label="说明"
                desc="关掉后角色不知道现在几点，也不会说「这么晚还不睡」这类话；老客户端不受影响"
                tone="keep"
                off
              >
                <span class="st__ro">—</span>
              </SetRow>
            </SetSection>
          </template>

          <!-- ============ 3. 干员记忆 ============ -->
          <!-- 跨对话「人情账」：全局开关 + 按干员分组的条目列表（可删）+ 新建对话时的本局选择。
               实现全在 MemoryPanel.vue 里，这里只挂一行 —— 设置页本身别长胖。 -->
          <MemoryPanel v-if="activeTab === 'memory'" />

          <!-- ============ 3. 实验性功能 ============ -->
          <template v-if="activeTab === 'experimental'">
            <SetSection title="模型能力">
              <SetRow
                label="思考模式"
                :hint="thinkHint.text"
                :tone="thinkHint.level"
              >
                <SegControl
                  :model-value="settingsStore.thinkEnabled ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  aria-label="思考模式"
                  @update:model-value="(v) => (settingsStore.thinkEnabled = v === 'on')"
                />
              </SetRow>

              <SetRow
                label="使用新版提示词"
                :hint="useNewPromptHint.text"
                :tone="useNewPromptHint.level"
                :off="newPromptBlocked"
              >
                <SegControl
                  :model-value="settingsStore.useNewPrompt ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  :disabled="newPromptBlocked"
                  :title="newPromptTitle || ''"
                  aria-label="使用新版提示词"
                  @update:model-value="(v) => (settingsStore.useNewPrompt = v === 'on')"
                />
              </SetRow>
            </SetSection>

            <!-- 【2026-11-18 P0】「知识源」整块删除：联网搜索 / RAG 语料库两个开关
                 都已下线（新后端不做联网检索，只走世界书）。 -->

            <SetSection title="智能总结">
              <SetRow
                label="智能总结"
                :hint="summaryHint.text"
                :tone="summaryHint.level"
                :off="summaryBlocked"
              >
                <SegControl
                  :model-value="summaryDraft.enabled ? 'on' : 'off'"
                  :options="BOOL_SEG"
                  :disabled="summaryBlocked"
                  :title="summaryTitle || ''"
                  aria-label="智能总结"
                  @update:model-value="(v) => (summaryDraft.enabled = v === 'on')"
                />
              </SetRow>

              <SetRow
                v-if="summaryBlocked"
                label="智能总结当前不可用"
                desc="当前模式下前端不生成总结；切到「默认模式」或「自定义 API」单聊即可生效"
              >
                <span class="st__ro">—</span>
              </SetRow>

              <!-- 触发阈值精细度（2026-11-14 追加）：原「总结 API 配置」面板已移除。
                   默认模式下这些只对前端链路有意义 → 隐藏（服务端自己压，见 docs/08） -->
              <template v-if="summaryDraft.enabled && !summaryServerManaged">
                <!-- 运行参数（2026-11-14）：展示真实 usage 与当前请求历史保留轮数 -->
                <SetRow
                  label="运行参数"
                  hint="上次请求占用优先取真实 usage.prompt_tokens；裁剪后为按新历史重算的估算值"
                  tone="keep"
                >
                  <span class="st__ro">{{ summaryRuntimeLine }}</span>
                </SetRow>

                <!-- 自定义 API 模式：总结由前端直连用户配置的端点，长度随窗口动态变化 -->
                <SetRow
                  v-if="apiDraft.apiMode === 'custom'"
                  label="自定义总结长度"
                  hint="上下文 × 10%（token）× 1.6 字/token，夹在 [200, 1200] 字；总结直连上方「API 配置」的自定义端点"
                  tone="keep"
                >
                  <span class="st__ro">{{ summaryCustomLengthLine }}</span>
                </SetRow>

                <SetRow
                  label="精细度调整"
                  hint="上一轮真实 prompt 占用达到窗口的该百分比时触发总结；调低更省上下文，但总结更频繁"
                  tone="keep"
                  stack
                >
                  <div class="st__stack">
                    <div class="st__range-head">
                      <span class="st__range-value">{{ summaryTriggerCurrent }}%</span>
                      <span class="st__ro">{{ summaryTriggerTokenLine }}</span>
                    </div>
                    <input
                      class="st__range"
                      type="range"
                      :min="summaryTriggerBounds.min"
                      :max="summaryTriggerBounds.max"
                      step="1"
                      :value="summaryTriggerCurrent"
                      aria-label="总结触发阈值精细度"
                      @input="onSummaryTriggerInput"
                    />
                    <div class="st__range-scale">
                      <span>最低 {{ summaryTriggerBounds.min }}%（更省上下文，但总结更频繁）</span>
                      <span>最高 {{ summaryTriggerBounds.max }}%（推荐）</span>
                    </div>
                    <div class="st__ro">{{ summaryPrecisionLine }}</div>
                    <div v-if="summaryTriggerNotice" class="st__range-notice">
                      {{ summaryTriggerNotice }}
                    </div>
                  </div>
                </SetRow>
              </template>
            </SetSection>
          </template>

          <!-- ============ 4. 数据管理 ============ -->
          <div v-if="activeTab === 'data'" class="st__data">
            <DataManagerDialog :open="open" embedded @goto-account="onSelectTab('account')" />
          </div>

          <!-- ============ 5. 关于 ============ -->
          <!-- 原「免责声明」页重做(3.2.2 的版式):头图 + 版本 → 社区二维码 →
               开源仓库 → 更新日志 → 版权与免责入口。
               条款正文**不在这一页**:它只存在于 DisclaimerDialog,与开屏公告
               共用同一个组件,两处各写一份迟早会走散。 -->
          <AboutPanel v-if="activeTab === 'about'" />

        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

// =============================================================================
// 设置面板(全屏分类面板)
// -----------------------------------------------------------------------------
// 结构:遮罩 + 全屏窗口(顶部页签栏 / 内容滚动区)。
// 通用的面板外观(遮罩、页签、分组标题、设置行、分段控件、胶囊按钮与输入框)
// 全部来自全局设计系统 `styles/panel.scss`(`.pn-` 前缀),这里只写本页特有
// 的栅格与少量补充样式(只读值、按钮行、滑块、危险按钮、文档与关于页)。
// =============================================================================

.st {
  position: fixed;
  inset: 0;
  z-index: 500;

  // 设置面板的遮罩比公告页更不透明:把后面的聊天界面压暗,控件更清楚
  .pn-scrim {
    background: rgba(8, 9, 10, 0.88);
  }
}

.st__win {
  // 顶部安全区：页签栏贴着安全区下沿，不再钻到状态栏/挖孔底下
  // （--safe-top 由 index.html 里那段脚本从原生 WindowInsets 写进来，网页端为 0）
  padding-top: var(--safe-top, 0px);
}

.st__top {
  position: relative;
  display: flex;
  flex-shrink: 0;
  align-items: flex-start;
  justify-content: center;
  min-height: 56px;
}

// 左上角:与主界面顶部一致的「小图 + //BAKER/…」标题
.st__brand {
  position: absolute;
  left: 22px;
  top: 6px;
  pointer-events: none;
  user-select: none;
}

.st__brand-deco {
  display: block;
  width: 63px;
  height: 12px;
  opacity: 0.85;
}

.st__brand-title {
  margin: 5px 0 0;
  color: $color-text-primary;
  font-family: $font-harmony;
  font-size: 21px;
  font-weight: 500;
  line-height: 1;
  letter-spacing: -0.5px;
  white-space: nowrap;
}

.st__body {
  // 内容与页签栏之间留一点呼吸
  padding-top: 6px;
}

// ---- 只读值(当前干员 / 内置端点等) ----------------------------------------
.st__ro {
  color: #e6e6e6;
  font-size: 13.5px;
  white-space: nowrap;
}

// 「每次检索条数」下拉:只占一小段,不拉满控件列
.st__topk {
  width: 132px;
}

// ---- 控件列里的纵向堆叠 ----------------------------------------------------
.st__stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  min-width: 0;
}

// ---- 按钮行 ----------------------------------------------------------------
.st__btnrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

/* 账号页：设备列表（一行一台：左边平台/版本，右边最后活跃） */
.st__devices {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
  width: 100%;
  font-family: $font-harmony;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.78);

  li {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }
}

.st__devices-time {
  color: rgba(255, 255, 255, 0.45);
  white-space: nowrap;
}

// ---- 取件码输入 / 领取结果（账号页） ---------------------------------------
// 取件码是 8 位字母数字，字距拉开 + 大写：用户要逐字核对，别挤在一起
.st__code-input {
  text-transform: uppercase;
  letter-spacing: 3px;
  font-variant-numeric: tabular-nums;

  // 取件码无效（本地格式错 / 后端 bad_claim_code）：红框，与红底提示配套
  &--invalid {
    border-color: #ff8f8f;
    background: rgba(255, 143, 143, 0.08);
  }
}

// 取件码失败的醒目提示：红底 + 左侧竖条（比普通 pn-hint 更抓眼）
.st__claim-error {
  margin: 0;
  padding: 8px 10px;
  border-left: 3px solid #ff6b6b;
  border-radius: 0 4px 4px 0;
  background: rgba(255, 107, 107, 0.12);
  color: #ffc9c9;
  font-family: $font-harmony;
  font-size: 13px;
  line-height: 1.65;
}

// 领取成功摘要：绿底（与错误红明确区分）
.st__claim-ok {
  margin: 12px 44px 0;
  padding: 8px 10px;
  border-left: 3px solid #6be39a;
  border-radius: 0 4px 4px 0;
  background: rgba(107, 227, 154, 0.1);
  color: #d7ffe8;
  font-family: $font-harmony;
  font-size: 13px;
  line-height: 1.65;
}

// ---- 总结精细度滑条(2026-11-14 追加) -------------------------------------
.st__range-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.st__range-value {
  color: #ffee34;
  font-size: 18px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}

.st__range-scale {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: rgba(255, 255, 255, 0.62);
  font-size: 12px;
  line-height: 1.4;
}

.st__range-notice {
  padding: 6px 10px;
  border-left: 3px solid #ffee34;
  border-radius: 0 3px 3px 0;
  background: rgba(255, 238, 52, 0.08);
  color: rgba(255, 255, 255, 0.9);
  font-size: 12.5px;
  line-height: 1.5;
}

// ---- 温度滑块:轨道压成细线,拇指用标识黄 ---------------------------------
.st__range {
  width: 100%;
  height: 20px;
  margin: 0;
  appearance: none;
  -webkit-appearance: none;
  background: transparent;
  cursor: pointer;

  &::-webkit-slider-runnable-track {
    height: 4px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.24);
  }

  &::-webkit-slider-thumb {
    width: 16px;
    height: 16px;
    margin-top: -6px;
    border: 0;
    border-radius: 50%;
    background: #ffee34;
    box-shadow: 0 0 4px rgba(0, 0, 0, 0.45);
    appearance: none;
    -webkit-appearance: none;
  }

  &::-moz-range-track {
    height: 4px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.24);
  }

  &::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border: 0;
    border-radius: 50%;
    background: #ffee34;
  }
}

// ---- 危险操作(重置全部) --------------------------------------------------
.st__danger {
  background: rgba(214, 74, 74, 0.22);
  color: #ffb3b3;
  box-shadow: 0 0 0 1.5px rgba(255, 120, 120, 0.42) inset;

  &:hover:not(:disabled) {
    background: rgba(214, 74, 74, 0.36);
    color: #ffd0d0;
  }
}

// ---- 数据管理页(内嵌组件,铺满内容宽度) ----------------------------------
.st__data {
  padding: 0 44px;

  // 组件本身带一层 16px 内边距(弹窗居中用的),内嵌时必须归零:
  // 否则整页内容比其它页多缩进 16px,左边缘对不齐。
  :deep(.dm) {
    padding: 0;
  }

  // 分组标题:与设置页各分类同一套(黄色竖条 + 通栏细线)
  // 数值逐项对齐 .pn-sec__head + .pn-rows:字号 17 / 上 2px 下 10px,
  // 标题与下方内容之间留 8px(其它页由 .pn-rows 的 margin-top 提供)
  :deep(.dm__group) {
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 12px 0 8px;
    padding: 2px 0 10px;
    color: #ffef00;
    font-size: 17px;
    font-weight: 700;
    line-height: 1.4;

    &::before {
      content: '';
      flex: 0 0 4px;
      width: 4px;
      height: 20px;
      background: #ffef00;
    }

    &::after {
      content: '';
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      height: 1px;
      background: rgba(255, 255, 255, 0.32);
    }
  }

  // 顶部统计行后面紧跟的第一个分组标题:不要再加间距
  :deep(.dm__stats + .dm__group) {
    margin-top: 0;
  }

  :deep(.dm__group--danger) {
    color: #ff8f8f;

    &::before {
      background: #ff8f8f;
    }
  }

  :deep(.dm__stats) {
    margin: 0 0 12px;
    color: rgba(255, 255, 255, 0.72);
    font-size: 13px;
  }

  :deep(.dm__subtitle) {
    // 标题下方那行灰字(云同步说明):离分割线再远一点,别贴着看
    margin: 6px 0 10px;
    color: rgba(255, 255, 255, 0.6);
    font-size: 13px;
  }

  // 按钮组做成一张卡片,与设置行同色、同内边距、同圆角、同最小高度
  // (圆角要 !important:弹窗那套 shell 会给子元素强制直角)
  :deep(.dm__actions) {
    gap: 10px;
    min-height: 62px;
    padding: 10px 24px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 3px !important;
    background: #323234;
  }

  // 危险操作:一行排开(手机上自动换行),不再竖着堆四条
  :deep(.dm__actions--menu) {
    flex-flow: row wrap;
    gap: 10px;
  }

  :deep(.dm__divider) {
    display: none;
  }

  :deep(.dm__btn) {
    border-radius: 999px !important;
    padding: 6px 16px !important;
    background: #edebec !important;
    color: #373537 !important;
    font-size: 13.5px !important;
    line-height: 1.4 !important;
    box-shadow: 0 0 3px rgba(0, 0, 0, 0.27);
    transition: background-color 0.15s ease;

    &:hover:not(:disabled) {
      background: #e0e0e0 !important;
    }

    &:disabled {
      opacity: 0.45;
    }
  }

  :deep(.dm__btn--primary) {
    background: #2a2728 !important;
    color: #fefcfc !important;

    &:hover:not(:disabled) {
      background: #1e1c1c !important;
    }
  }

  :deep(.dm__btn--danger) {
    background: rgba(214, 74, 74, 0.22) !important;
    color: #ffb3b3 !important;

    &:hover:not(:disabled) {
      background: rgba(214, 74, 74, 0.36) !important;
    }
  }

  :deep(.dm__input) {
    border-radius: 999px !important;
    background: #edebec !important;
    color: #2c2c2c !important;
  }

  :deep(.dm__code-value) {
    color: #ffee34;
  }

  // 二次确认弹窗的正文在白底弹窗里,文字颜色由 DataManagerDialog 自己定,
  // 这里不要再覆盖(曾经写过 #fff,导致白底白字)
}

// ---------------------------------------------------------------------------
// 移动端
// ---------------------------------------------------------------------------
@media (max-width: 768px) {
  // 手机端不显示左上角的「//设置」与小图,把顶部整条让给页签
  .st__brand {
    display: none;
  }

  .st__top {
    min-height: 44px;
  }

  .st__data {
    padding-left: 12px;
    padding-right: 12px;
  }
}
</style>
