<script setup lang="ts">
/**
 * LoginView.vue —— 登录页。
 *
 * 只做三件事：收用户名密码、把错话翻成人话、说清楚"这扇门只给管理员开"。
 * 真正的登录在 App.vue（它要负责"登录成功后先验一次权限"），这里只抛事件。
 */
import { ref } from 'vue'

const props = defineProps<{
  busy: boolean
  /** 登录失败的一句话（后端给的中文，或者我们自己补的） */
  error: string
  /** 不是错误、但要让用户知道的一句话（"登录已过期""这个账号不是管理员"） */
  notice?: string
}>()

const emit = defineEmits<{ submit: [username: string, password: string] }>()

const username = ref('')
const password = ref('')
/** 本地校验：不填就不发请求，省一次 422 */
const localError = ref('')

function onSubmit() {
  localError.value = ''
  if (!username.value.trim()) {
    localError.value = '请填用户名'
    return
  }
  if (!password.value) {
    localError.value = '请填密码'
    return
  }
  emit('submit', username.value.trim(), password.value)
}
</script>

<template>
  <div class="pn-login">
    <div class="pn-login-card">
      <div class="ef-tri" aria-hidden="true"><i /><i /><i /></div>

      <p class="ef-eyebrow slash">Panel / Sign in</p>
      <h1 style="margin: 0.2rem 0 0.4rem; font-size: 1.75rem; font-weight: 800">后台登录</h1>
      <p class="pn-note" style="margin-bottom: 1.1rem">
        用管理员账号登录。数据集中的地方都在这里：账号、干员、密钥池、配置。
      </p>

      <form class="ef-form" novalidate @submit.prevent="onSubmit">
        <label class="pn-field">
          <span>用户名</span>
          <input
            v-model="username"
            class="pn-input"
            name="username"
            autocomplete="username"
            :disabled="props.busy"
            placeholder="管理员用户名"
          />
        </label>

        <label class="pn-field">
          <span>密码</span>
          <input
            v-model="password"
            class="pn-input"
            name="password"
            type="password"
            autocomplete="current-password"
            :disabled="props.busy"
            placeholder="登录密码"
          />
        </label>

        <p v-if="localError || props.error" class="ef-notice" data-flag="出错">
          {{ localError || props.error }}
        </p>
        <p v-else-if="props.notice" class="ef-notice" data-flag="提示">{{ props.notice }}</p>

        <p class="ef-actions" style="margin: 0.2rem 0 0">
          <button class="ef-btn go" type="submit" :aria-busy="props.busy" :disabled="props.busy">
            {{ props.busy ? '登录中…' : '登录' }}
          </button>
        </p>
      </form>

      <div class="pn-login-foot">
        <span>BakerAI · panel.peilika.beer</span>
        <span>仅管理员</span>
      </div>
    </div>
  </div>
</template>
