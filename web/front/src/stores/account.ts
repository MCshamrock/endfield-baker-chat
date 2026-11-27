// =============================================================================
// account store —— 当前是谁（访客 / 账号）
// -----------------------------------------------------------------------------
// 新后端的身份有两层，别混：
//   · **设备**：匿名 token，第一请求自动注册（utils/apiClient）。
//   · **账号**：用户名 + 密码，登录后把设备挂到账号上；token 不换。
//
// 所以这里只维护"账号层"的状态：`unknown → guest / user`。
// 聊天请求永远带同一张设备 token，登录/登出**不需要**重建任何链路。
//
// 三条行为照 docs/03 §6：
//   1. 启动时 refresh() 一次，决定设置页显示"登录"还是昵称；
//   2. 登录/注册成功不重拉会话，只在收编了记录时给一句提示；
//   3. 登出只清账号状态 —— 本地聊天存档是玩家自己的数据，不删。
// =============================================================================

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  changePassword as apiChangePassword,
  fetchMe,
  fetchOwnerData,
  loginAccount,
  logoutAccount,
  registerAccount,
  type AccountDevice,
  type AccountUser,
} from '../utils/auth'
import { ApiError, friendlyError } from '../utils/apiClient'
import { devInfo, devWarn } from '../utils/logger'

/** 账号层状态：还没问过服务端 / 访客 / 已登录 */
export type AccountState = 'unknown' | 'guest' | 'user'

/** 每个动作的返回：给界面一句话，不抛异常（界面不用写 try/catch） */
export interface ActionResult {
  ok: boolean
  message: string
  /**
   * 失败时的业务错误码（后端 `error.code`，例如 bad_claim_code）
   *
   * 为什么需要：界面除了显示 message，有时还要做别的反应 ——
   * 比如 `bad_claim_code` 要单独用醒目的一行提示，并且**不能清空**用户填的
   * 用户名 / 密码（他只要换个码重试就行）。
   */
  code?: string
}

export const useAccountStore = defineStore('account', () => {
  /** 当前状态 */
  const state = ref<AccountState>('unknown')
  /** 已登录时的账号信息 */
  const user = ref<AccountUser | null>(null)
  /** 账号名下的设备（访客为空） */
  const devices = ref<AccountDevice[]>([])
  /** 是否有请求在飞（按钮禁用用） */
  const busy = ref(false)
  /** 最近一次操作的提示（成功/失败都写这里；界面只渲染它） */
  const notice = ref('')
  /** 提示是否算"错误"（决定颜色） */
  const noticeIsError = ref(false)
  /** 最近一次登录/注册收编了多少条设备上已有记录 */
  const adopted = ref(0)
  /**
   * 「刚刚真的登录/注册成功」的次数
   *
   * 为什么要单独一个计数器：启动时 `refresh()` 也会把状态从 unknown 变成 user
   * （cookie/token 还在），但那是"恢复身份"，不是"用户刚登录"。
   * 登录后要自动上传本机存档（见 stores/list.ts），只该在**真的登录**时做一次 ——
   * 拿状态变化当信号的话，每次开机都会白读一遍整个卡库。
   */
  const loginSeq = ref(0)

  // ---------------------------------------------------------------------
  // 「有记录就必须先登录」这道墙的事实依据
  //
  // 背景（2026-11-18 事故）：聊天记录先挂在**设备**名下（`d:<device_id>`），
  // 设备凭证只存在这台浏览器里。清一次缓存 / 换个浏览器 / 换台手机，
  // 那批记录就跟丢了 —— 用户看到的是"我的记录全没了"。
  // 所以在**已经有记录**、却还没登录的时候，先把人拦下来登录/注册：
  // 登录会把设备名下的记录改挂到账号（服务端 adopt_owned，只改一列，不搬数据）。
  //
  // 只有真的查到 >0 才拦：没有记录的新人照旧直接用，不打扰。
  // ---------------------------------------------------------------------
  /** 这份身份名下的会话数（0 = 没有历史，不用拦） */
  const ownedConversations = ref(0)
  /** 这份身份名下的消息数（只用于文案，让用户知道"有多少东西"） */
  const ownedMessages = ref(0)
  /** 查过没有（没查完之前不显示墙，避免闪一下又消失） */
  const historyChecked = ref(false)
  /** 正在查 */
  const historyBusy = ref(false)

  /**
   * 问一次"我名下有多少记录"
   *
   * 失败时**不拦人**：网络一抖就把整站挡在门外，比记录风险更糟。
   * 失败按"没有记录"处理（这是有意的降级，不是漏判）。
   */
  async function checkOwnedHistory(force = false): Promise<void> {
    if (historyBusy.value) return
    if (historyChecked.value && !force) return
    historyBusy.value = true
    try {
      const data = await fetchOwnerData()
      ownedConversations.value = data.conversations
      ownedMessages.value = data.messages
      devInfo('[account] 名下记录', data.conversations, '段 /', data.messages, '条')
    } catch (err) {
      devWarn('[account] 查名下记录失败（按没有处理，不拦人）:', err)
      ownedConversations.value = 0
      ownedMessages.value = 0
    } finally {
      historyChecked.value = true
      historyBusy.value = false
    }
  }

  /**
   * 要不要显示"必须先登录"的墙
   *
   * 【2026-11-20 产品口径】改成"本地优先、账号可选"：**不再强制登录**。
   * 记录在这台设备的存档里就有，登录是"要跨设备 / 要备份"时才做的动作
   * （登录后会自动把本机存档传上云，见 stores/list.ts 的 onLoggedIn）。
   *
   * 判据留着（`ownedConversations` 仍然会查，设置页的账号卡片在用），
   * 这里只是不再拿它挡人。真要恢复强制登录，把 `FORCE_LOGIN_WALL` 改回 true。
   */
  const FORCE_LOGIN_WALL = false
  const needLoginWall = computed(
    () =>
      FORCE_LOGIN_WALL &&
      state.value === 'guest' &&
      historyChecked.value &&
      ownedConversations.value > 0,
  )

  function setNotice(message: string, isError = false): void {
    notice.value = message
    noticeIsError.value = isError
  }

  /** 清掉提示（切换页签 / 再次输入时用） */
  function clearNotice(): void {
    notice.value = ''
    noticeIsError.value = false
  }

  /**
   * 问服务端"我是谁"
   *
   * @param silent true = 失败不写提示（启动时调，网络不通不该弹错误）
   * @param force  true = 即使 busy 也执行（登录/注册/改密成功后顺手刷新设备列表，
   *               此时 busy 仍为 true，不加这条会被顶部的守卫直接跳过）
   */
  async function refresh(silent = false, force = false): Promise<void> {
    if (busy.value && !force) return
    if (!force) busy.value = true
    try {
      const me = await fetchMe()
      if (me.kind === 'user' && me.user) {
        state.value = 'user'
        user.value = me.user
        devices.value = me.devices
      } else {
        state.value = 'guest'
        user.value = null
        devices.value = []
      }
      devInfo('[account] 当前身份', state.value, user.value?.username ?? '')
    } catch (err) {
      // 拉不到就保持 unknown：界面显示"正在连接"，而不是误判成访客
      if (!silent) setNotice(friendlyError(err), true)
      devWarn('[account] 拉取身份失败:', err)
    } finally {
      if (!force) busy.value = false
    }
  }

  /** 登录 */
  async function login(username: string, password: string): Promise<ActionResult> {
    if (busy.value) return { ok: false, message: '正在处理，请稍候' }
    busy.value = true
    clearNotice()
    try {
      const result = await loginAccount(username, password)
      user.value = result.user
      state.value = 'user'
      adopted.value = result.adopted_conversations
      loginSeq.value += 1
      const suffix = result.adopted_conversations > 0
        ? `（已把 ${result.adopted_conversations} 条记录并入账号）`
        : ''
      setNotice(`登录成功：${result.user.display_name || result.user.username}${suffix}`)
      // 设备列表顺手刷新（失败不影响登录本身）
      void refresh(true, true)
      return { ok: true, message: notice.value }
    } catch (err) {
      const message = friendlyError(err)
      setNotice(message, true)
      return { ok: false, message }
    } finally {
      busy.value = false
    }
  }

  /** 注册（注册即登录） */
  async function register(
    username: string,
    password: string,
    displayName = '',
    claimCode = '',
  ): Promise<ActionResult> {
    if (busy.value) return { ok: false, message: '正在处理，请稍候' }
    busy.value = true
    clearNotice()
    try {
      // claimCode 非空时，后端会在建账号的同时把那份包领到账号名下；
      // 码无效/过期会整次回滚（400 bad_claim_code），throw 走下面的 catch。
      const result = await registerAccount(username, password, displayName, claimCode)
      user.value = result.user
      state.value = 'user'
      adopted.value = result.adopted_conversations
      loginSeq.value += 1
      const suffix = result.adopted_conversations > 0
        ? `（已把 ${result.adopted_conversations} 条记录并入账号）`
        : ''
      setNotice(`注册成功：${result.user.display_name || result.user.username}${suffix}`)
      void refresh(true, true)
      return { ok: true, message: notice.value }
    } catch (err) {
      const message = friendlyError(err)
      setNotice(message, true)
      return { ok: false, message, code: err instanceof ApiError ? err.code : undefined }
    } finally {
      busy.value = false
    }
  }

  /** 退出账号 */
  async function logout(): Promise<ActionResult> {
    if (busy.value) return { ok: false, message: '正在处理，请稍候' }
    busy.value = true
    clearNotice()
    try {
      await logoutAccount()
      state.value = 'guest'
      user.value = null
      devices.value = []
      setNotice('已退出登录。聊天记录仍在这台设备的存档里，重新登录可再看到服务端数据。')
      return { ok: true, message: notice.value }
    } catch (err) {
      const message = friendlyError(err)
      setNotice(message, true)
      return { ok: false, message }
    } finally {
      busy.value = false
    }
  }

  /** 改密码（成功会踢掉其它设备） */
  async function changePassword(oldPassword: string, newPassword: string): Promise<ActionResult> {
    if (busy.value) return { ok: false, message: '正在处理，请稍候' }
    busy.value = true
    clearNotice()
    try {
      await apiChangePassword(oldPassword, newPassword)
      setNotice('密码已修改；其它设备已被登出，这台设备不用重新登录。')
      void refresh(true, true)
      return { ok: true, message: notice.value }
    } catch (err) {
      const message = friendlyError(err)
      setNotice(message, true)
      return { ok: false, message }
    } finally {
      busy.value = false
    }
  }

  return {
    // state
    state,
    user,
    devices,
    busy,
    notice,
    noticeIsError,
    adopted,
    loginSeq,
    ownedConversations,
    ownedMessages,
    historyChecked,
    historyBusy,
    needLoginWall,
    // actions
    refresh,
    checkOwnedHistory,
    login,
    register,
    logout,
    changePassword,
    clearNotice,
    setNotice,
  }
})
