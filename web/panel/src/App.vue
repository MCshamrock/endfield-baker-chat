<script setup lang="ts">
/**
 * App.vue —— 面板的壳：登录闸门、命令栏、分区路由。
 *
 * 启动顺序（三段，缺一段就会出现"闪一下登录页"或者"没权限的人看到空表格"）：
 *   1. 读 localStorage 里的主题与"上次是谁"，先把主题定下来（index.html 已经先定了一次）；
 *   2. 手里有 token 就**打一次后台接口**验证权限 —— 因为登录响应里没有 role，
 *      "是不是管理员"只能这样问出来：200 是，403 不是，401 是过期；
 *   3. 验证通过才显示面板，否则回到登录页并说清楚原因。
 *
 * 401 的处理只有一条路径：`attachUnauthorizedHandler` → 清会话 → 回登录页。
 * 任何一个页面里的任何一个请求 401，都走这条路，不用各页面自己判断。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import LoginView from './components/LoginView.vue'
import OverviewView from './views/OverviewView.vue'
import UsersView from './views/UsersView.vue'
import CharactersView from './views/CharactersView.vue'
import ConfigView from './views/ConfigView.vue'
import KeysView from './views/KeysView.vue'
import UsageView from './views/UsageView.vue'
import ReasoningView from './views/ReasoningView.vue'
import { api, ApiError, attachUnauthorizedHandler } from './lib/api.ts'
import { localZoneLabel } from './lib/format.ts'
import { SECTIONS, readRoute, sectionOf, type SectionKey } from './lib/nav.ts'
import { applyTheme, clearUser, loadUser, readTheme, saveUser, type SavedUser, type Theme } from './lib/session.ts'

const session = ref<SavedUser | null>(null)
/** 启动验证中：这一小段时间既不显示登录页也不显示面板，避免闪 */
const booting = ref(true)
const route = ref<SectionKey>(readRoute())
const theme = ref<Theme>(readTheme())

const loginBusy = ref(false)
const loginError = ref('')
const loginNotice = ref('')

/** 页头状态条上的本地时钟（真实读数，不是装饰动画） */
const clock = ref(new Date())
let clockTimer = 0

const section = computed(() => sectionOf(route.value))

const clockText = computed(() => {
  const d = clock.value
  const p = (n: number) => (n < 10 ? `0${n}` : String(n))
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
})

/** 401 的唯一落点：清掉会话，回登录页，并告诉用户为什么 */
function onAuthLost() {
  if (!session.value) return
  dropSession('登录已过期，请重新登录')
}

function dropSession(notice = '') {
  session.value = null
  clearUser()
  // 关键：401 之后必须把设备 token 一起丢掉。
  // 只清 user 的话，下次登录会拿这张已作废的 token 去调 /v1/auth/login，
  // 结果连正确密码也被判 401（审计 B11 实测的"登不回来"）。
  api.clearToken()
  loginNotice.value = notice
}

async function bootstrap() {
  if (!api.hasToken()) {
    booting.value = false
    return
  }
  try {
    // 最小代价的权限探针：limit=1 只取一行，但足以区分 200 / 401 / 403
    await api.listUsers({ limit: 1 })
    const saved = loadUser()
    // 名字丢了也能进（权限是 token 决定的），只是页头显示"管理员"
    session.value = saved ?? { username: 'admin', displayName: '' }
  } catch (err) {
    if (err instanceof ApiError && err.isForbidden) {
      // 是这个人登着、但角色不是 admin：token 不能再留着（否则每次刷新都 403）
      await api.logout()
      dropSession('这个账号没有管理员权限，面板只对管理员开放')
    } else if (err instanceof ApiError && err.isUnauthorized) {
      // 启动期 session 还是空的，onAuthLost() 会提前 return —— 于是死 token 留在
      // localStorage 里，下次登录被迫多走一次 bad_token 重试。这里显式清掉，做到
      // "401 即清"，也让刷新后的状态和界面对得上。
      api.clearToken()
      loginNotice.value = '登录已过期，请重新登录'
    } else if (err instanceof ApiError) {
      loginNotice.value = err.message
    }
  } finally {
    booting.value = false
  }
}

async function onLogin(username: string, password: string) {
  loginBusy.value = true
  loginError.value = ''
  loginNotice.value = ''
  try {
    const user = await api.login(username, password)
    // 登录只证明"密码对"，不证明"是管理员"。再打一次后台接口才算数。
    try {
      await api.listUsers({ limit: 1 })
    } catch (err) {
      if (err instanceof ApiError && err.isForbidden) {
        await api.logout()
        loginError.value = '这个账号不是管理员。面板只对管理员开放，请换账号。'
        return
      }
      throw err
    }
    const saved: SavedUser = { username: user.username, displayName: user.display_name }
    saveUser(saved)
    session.value = saved
    route.value = readRoute()
  } catch (err) {
    loginError.value = err instanceof ApiError ? err.message : '登录失败，稍后再试'
  } finally {
    loginBusy.value = false
  }
}

async function onLogout() {
  await api.logout()
  dropSession()
}

function onSelfRenamed(displayName: string) {
  if (!session.value) return
  const next = { ...session.value, displayName }
  session.value = next
  saveUser(next)
}

function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  applyTheme(theme.value)
}

function onHashChange() {
  route.value = readRoute()
  window.scrollTo({ top: 0 })
}

onMounted(() => {
  attachUnauthorizedHandler(onAuthLost)
  applyTheme(theme.value)
  window.addEventListener('hashchange', onHashChange)
  clockTimer = window.setInterval(() => {
    clock.value = new Date()
  }, 1000)
  void bootstrap()
})

onBeforeUnmount(() => {
  window.removeEventListener('hashchange', onHashChange)
  window.clearInterval(clockTimer)
})
</script>

<template>
  <!-- 启动验证中：一块安静的骨架，不闪登录页 -->
  <div v-if="booting" class="pn-login">
    <div class="pn-login-card">
      <div class="ef-tri" aria-hidden="true"><i /><i /><i /></div>
      <p class="ef-eyebrow slash">Panel / Booting</p>
      <p class="pn-note" style="margin-top: 0.6rem">正在确认登录状态…</p>
    </div>
  </div>

  <LoginView
    v-else-if="!session"
    :busy="loginBusy"
    :error="loginError"
    :notice="loginNotice"
    @submit="onLogin"
  />

  <div v-else class="pn-shell">
    <!-- 三色条只出现在屏幕最顶端，表示"这一屏从这里开始" -->
    <div class="ef-tri" aria-hidden="true"><i /><i /><i /></div>

    <header class="ef-cmdbar">
      <a class="brand pn-cmd-brand" href="#/overview">
        <span class="seal">B</span>BAKER PANEL<em>后台</em>
      </a>
      <nav aria-label="后台分区">
        <a
          v-for="item in SECTIONS"
          :key="item.key"
          :href="`#/${item.key}`"
          :aria-current="route === item.key ? 'page' : undefined"
        >
          {{ item.zh }}<small>{{ item.en }}</small>
        </a>
      </nav>
      <div class="util pn-cmd-util">
        <div class="who">
          <b>{{ session.displayName || session.username }}</b>
          <small>管理员</small>
        </div>
        <button class="ef-btn small" type="button" @click="toggleTheme">
          {{ theme === 'dark' ? '浅色' : '深色' }}
        </button>
        <button class="ef-btn small" type="button" @click="onLogout">退出</button>
      </div>
    </header>

    <!-- 读数行：全是真的当前状态（谁、哪个时区、几点、在哪一页） -->
    <div class="pn-status">
      <span class="live">已登录 <b>{{ session.username }}</b></span>
      <span class="sep">│</span>
      <span>本地时区 <b>{{ localZoneLabel() }}</b></span>
      <span class="sep">│</span>
      <span>本地时间 <b>{{ clockText }}</b></span>
      <span class="sep">│</span>
      <span>当前分区 <b>{{ section.zh }}</b></span>
      <span style="margin-left: auto">BakerAI 后台 · 不显示任何密钥内容</span>
    </div>

    <main class="pn-main">
      <OverviewView v-if="route === 'overview'" />
      <UsersView v-else-if="route === 'users'" :me="session.username" @self-renamed="onSelfRenamed" />
      <CharactersView v-else-if="route === 'characters'" />
      <ConfigView v-else-if="route === 'config'" />
      <KeysView v-else-if="route === 'keys'" />
      <UsageView v-else-if="route === 'usage'" />
      <ReasoningView v-else-if="route === 'reasoning'" />
    </main>
  </div>
</template>
