<script setup lang="ts">
// =============================================================================
// LoginWall —— 「你已经有聊天记录，但这台设备还没登录」时挡在最前面的那一道墙
// -----------------------------------------------------------------------------
// 为什么要有它（2026-11-18 事故的收口动作）：
//   聊天记录是挂在**设备**名下的（`d:<device_id>`），设备凭证只存在这个浏览器里。
//   清缓存 / 换浏览器 / 换手机 → 记录就跟丢了，用户看到的是"我的记录全没了"。
//   所以：**已经有记录、却没登录账号**的设备，进来先登录或注册。
//   登录那一刻服务端会把设备名下的记录改挂到账号（只改一列，不搬数据），
//   从此换任何设备登录都能看见 —— 这是"数据不会丢"的唯一保证。
//
// 三条设计约束（都很重要，改之前先读）：
//   1. **没有记录的人不受影响**：`needLoginWall` 只在会话数 > 0 时为真，
//      新用户照旧"打开就能聊"。
//   2. **拉不到数量就不拦人**：网络抖动不能把整站挡在门外（见 account store 注释）。
//   3. **不给"以设备身份继续"的出口**：那等于允许数据继续裸奔在设备上。
//      真出问题（比如设备被别的账号占着）要给人一条能求助的路 —— 底部有群号。
// =============================================================================
import { computed, ref } from 'vue'

import { useAccountStore } from '../../stores/account'
import {
  PASSWORD_RULE_TEXT,
  USERNAME_RULE_TEXT,
  validatePassword,
  validateUsername,
} from '../../utils/auth'
import { ApiError, friendlyError } from '../../utils/apiClient'

const account = useAccountStore()

/**
 * 默认停在「注册」：能走到这道墙的人，基本就是"一直在用、但从来没注册过"。
 * 已经有账号的人会自己点上面那个「登录」。
 */
const mode = ref<'login' | 'register'>('register')

const username = ref('')
const password = ref('')
const confirm = ref('')
const displayName = ref('')
const claimCode = ref('')

/** 本地校验 / 服务端返回的错误，显示在表单下方 */
const localError = ref('')
/** 设备已被别的账号占用这类"卡死"的情况，需要单独一段醒目说明 */
const blocked = ref('')

const countText = computed(() => {
  const c = account.ownedConversations
  const m = account.ownedMessages
  if (c <= 0) return ''
  if (m > 0) return `这台设备上有 ${c} 段对话（共 ${m} 条消息）`
  return `这台设备上有 ${c} 段对话`
})

const submitLabel = computed(() => {
  if (account.busy) return '处理中…'
  return mode.value === 'register' ? '注册并登录' : '登录'
})

function switchMode(next: 'login' | 'register'): void {
  mode.value = next
  localError.value = ''
  blocked.value = ''
  account.clearNotice()
}

/** 提交：先本地校验，再交给 account store（它把异常都转成 message，不抛） */
async function submit(): Promise<void> {
  if (account.busy) return
  localError.value = ''
  blocked.value = ''

  if (mode.value === 'register') {
    const nameError = validateUsername(username.value)
    if (nameError) {
      localError.value = nameError
      return
    }
    const passError = validatePassword(password.value)
    if (passError) {
      localError.value = passError
      return
    }
    if (password.value !== confirm.value) {
      localError.value = '两次输入的密码不一样'
      return
    }
  } else if (!username.value.trim() || !password.value) {
    localError.value = '用户名和密码都要填'
    return
  }

  const result = mode.value === 'register'
    ? await account.register(username.value, password.value, displayName.value, claimCode.value)
    : await account.login(username.value, password.value)

  if (result.ok) return

  // 这台设备之前登录过**别的**账号：那时的记录属于那个账号。
  // 注册/登录新号都会被拒（服务端 409 device_taken），直接把出路告诉人。
  if (result.code === 'device_taken') {
    blocked.value =
      '这台设备之前登录过另一个账号，那段记录在那个账号里。' +
      '请用原账号登录；记不得用户名或密码，去群里找管理员帮忙。'
    return
  }
  localError.value = result.message
}

/** 兜底：store 里没接住的异常（例如 apiClient 自己抛的）也别静默 */
function noticeText(): string {
  return localError.value || account.notice
}

/** 只有"真错误"才染红，提示性文案用普通颜色 */
function isError(): boolean {
  return !!localError.value || account.noticeIsError
}

/** 错误码集中判断留个口子：以后要按 code 做别的反应，加在这里 */
void ApiError
void friendlyError
</script>

<template>
  <div class="lw" role="dialog" aria-modal="true" aria-labelledby="lw-title">
    <div class="lw__card">
      <p class="lw__eyebrow">需要先登录</p>
      <h2 id="lw-title" class="lw__title">这段聊天记录还没挂到账号上</h2>

      <p v-if="countText" class="lw__count">{{ countText }}</p>

      <p class="lw__body">
        它现在只存在这台设备的浏览器里。清一次缓存、换个浏览器或换台手机，
        就再也找不回来了。<strong>登录或注册一个账号，这段记录会自动归到账号名下</strong>，
        以后在任何设备上登录都能看到。
      </p>

      <div class="lw__tabs" role="tablist">
        <button
          class="lw__tab"
          :class="{ 'lw__tab--on': mode === 'login' }"
          type="button"
          role="tab"
          :aria-selected="mode === 'login'"
          @click="switchMode('login')"
        >登录</button>
        <button
          class="lw__tab"
          :class="{ 'lw__tab--on': mode === 'register' }"
          type="button"
          role="tab"
          :aria-selected="mode === 'register'"
          @click="switchMode('register')"
        >注册</button>
      </div>

      <div class="lw__form">
        <label class="lw__field">
          <span class="lw__label">用户名</span>
          <input
            v-model="username"
            class="pn-input"
            type="text"
            autocomplete="username"
            :placeholder="mode === 'register' ? USERNAME_RULE_TEXT : '用户名'"
            :disabled="account.busy"
            @keyup.enter="submit"
          />
        </label>

        <label class="lw__field">
          <span class="lw__label">密码</span>
          <input
            v-model="password"
            class="pn-input"
            type="password"
            :autocomplete="mode === 'register' ? 'new-password' : 'current-password'"
            :placeholder="mode === 'register' ? PASSWORD_RULE_TEXT : '密码'"
            :disabled="account.busy"
            @keyup.enter="submit"
          />
        </label>

        <template v-if="mode === 'register'">
          <label class="lw__field">
            <span class="lw__label">再输一次密码</span>
            <input
              v-model="confirm"
              class="pn-input"
              type="password"
              autocomplete="new-password"
              :disabled="account.busy"
              @keyup.enter="submit"
            />
          </label>

          <label class="lw__field">
            <span class="lw__label">昵称（可选）</span>
            <input
              v-model="displayName"
              class="pn-input"
              type="text"
              :disabled="account.busy"
              @keyup.enter="submit"
            />
          </label>

          <label class="lw__field">
            <span class="lw__label">取件码（可选）</span>
            <input
              v-model="claimCode"
              class="pn-input"
              type="text"
              autocomplete="off"
              spellcheck="false"
              placeholder="在别的设备上传数据拿到的 8 位码"
              :disabled="account.busy"
              @keyup.enter="submit"
            />
          </label>
        </template>
      </div>

      <p v-if="blocked" class="lw__blocked">{{ blocked }}</p>
      <p v-else-if="noticeText()" class="lw__notice" :class="{ 'lw__notice--bad': isError() }">
        {{ noticeText() }}
      </p>

      <button
        class="pn-pill pn-pill--primary lw__submit"
        type="button"
        :disabled="account.busy"
        @click="submit"
      >{{ submitLabel }}</button>

      <details class="lw__why">
        <summary>为什么一定要登录？</summary>
        <p>
          你的聊天记录目前是跟着"这台设备的浏览器"走的，不跟人走。
          换设备、清缓存、甚至换个浏览器打开，都会变成一张空列表 ——
          今天的记录丢失就是这么来的。账号是唯一能把记录和你绑在一起的东西。
        </p>
        <p>
          登录只会把这台设备上已有的记录**改挂到账号名下**，不会删、不会复制、
          不会覆盖你已经有的任何一段对话。
        </p>
      </details>

      <p class="lw__help">
        登不上、忘了密码、或者提示设备被占用 —— 去群里找管理员，说明这台设备的情况。
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 层级：压过界面（设置 300 / 弹窗 500 级），但低于开屏公告与更新弹窗（9999+），
   这样"出事了要先看公告"的路径不会被这道墙堵死。 */
.lw {
  position: fixed;
  inset: 0;
  z-index: 9000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  overflow-y: auto;
  background: rgba(12, 14, 13, 0.72);
  backdrop-filter: blur(6px);
}

.lw__card {
  width: min(440px, 100%);
  max-height: calc(100vh - 48px);
  overflow-y: auto;
  padding: 26px 24px 22px;
  border-radius: 18px;
  background: #f6f4ef;
  color: #22262a;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.38);
}

.lw__eyebrow {
  margin: 0 0 8px;
  font-size: 12px;
  letter-spacing: 0.14em;
  color: #8a6a1f;
}

.lw__title {
  margin: 0 0 10px;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.35;
}

.lw__count {
  margin: 0 0 10px;
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(107, 227, 154, 0.22);
  font-size: 14px;
  font-weight: 500;
}

.lw__body {
  margin: 0 0 16px;
  font-size: 13.5px;
  line-height: 1.7;
  color: #3c4248;
}

.lw__tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 14px;
  padding: 3px;
  border-radius: 10px;
  background: rgba(34, 38, 42, 0.07);
}

.lw__tab {
  flex: 1;
  padding: 7px 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  font-size: 13.5px;
  color: #4a5158;
  cursor: pointer;
}

.lw__tab--on {
  background: #fff;
  color: #1c2024;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
}

.lw__form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.lw__field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.lw__label {
  font-size: 12.5px;
  color: #5a6169;
}

.lw__form :deep(.pn-input) {
  width: 100%;
  box-sizing: border-box;
  background: #fff;
  color: #1c2024;
  border-color: rgba(34, 38, 42, 0.18);
}

.lw__notice,
.lw__blocked {
  margin: 12px 0 0;
  padding: 9px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  background: rgba(34, 38, 42, 0.06);
  color: #3c4248;
}

.lw__notice--bad,
.lw__blocked {
  background: rgba(229, 72, 77, 0.12);
  color: #a3242a;
}

.lw__submit {
  width: 100%;
  margin-top: 16px;
}

.lw__why {
  margin-top: 16px;
  font-size: 12.5px;
  line-height: 1.7;
  color: #5a6169;
}

.lw__why summary {
  cursor: pointer;
  color: #2c6e4a;
}

.lw__why p {
  margin: 8px 0 0;
}

.lw__help {
  margin: 14px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: #7b828a;
}
</style>
