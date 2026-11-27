<script setup lang="ts">
/**
 * ReasoningView.vue —— 思考链（排障用，只给管理员看）。
 *
 * 为什么单独一页、而且要手填 public_id：思考链会复述上下文，等于"半个记忆"，
 * 后端注释里写得很清楚——绝不能开给普通客户端。所以这里不做列表、不做搜索，
 * 必须拿着具体会话 id 来查，减少"顺手翻一翻别人聊天"的机会。
 *
 * 展示上：一条消息一块，默认折叠，展开才看得到 thinking 原文。
 * 等宽字体 + 保留换行，因为模型吐出来的思考是带缩进和空行的。
 */
import { computed, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { api, ApiError } from '../lib/api.ts'
import { fmtDateTime, fmtInt } from '../lib/format.ts'
import type { ReasoningResult } from '../lib/types.ts'

const publicId = ref('')
const limit = ref<20 | 50 | 100>(20)
const loading = ref(false)
const error = ref('')
const result = ref<ReasoningResult | null>(null)
/** 查过一次之后才显示"空"状态；没查过显示的是引导语 */
const searched = ref(false)

const items = computed(() => result.value?.items ?? [])

async function query() {
  const id = publicId.value.trim()
  error.value = ''
  if (!id) {
    error.value = '先把会话的 public_id 填上（32 位十六进制，客户端「关于」或后台用户页能查到）'
    return
  }
  loading.value = true
  try {
    result.value = await api.reasoning(id, limit.value)
    searched.value = true
  } catch (err) {
    result.value = null
    searched.value = true
    error.value =
      err instanceof ApiError
        ? err.status === 404
          ? '没有这个会话 —— id 对不上，或者它属于另一个后端'
          : err.message
        : '查询失败'
  } finally {
    loading.value = false
  }
}

function reset() {
  publicId.value = ''
  result.value = null
  error.value = ''
  searched.value = false
}
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>思考链 <small>Reasoning</small></h1>
      <p>按会话 public_id 查最近的思考过程。只给管理员看，别外传。</p>
    </header>

    <p class="ef-notice" data-flag="注意">
      思考过程会复述上下文，等于半个记忆。这一页的内容不要截图外发，也不要贴到群里。
    </p>

    <form class="pn-toolbar pn-mt" @submit.prevent="query">
      <label class="pn-field grow">
        <span>会话 public_id</span>
        <input
          v-model="publicId"
          class="pn-input"
          placeholder="例如 8c1a…（32 位十六进制）"
          spellcheck="false"
          :disabled="loading"
        />
      </label>
      <label class="pn-field" style="width: auto">
        <span>条数</span>
        <select v-model.number="limit" class="pn-select inline" :disabled="loading">
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
      <button class="ef-btn go" type="submit" :aria-busy="loading" :disabled="loading">
        {{ loading ? '查询中…' : '查询' }}
      </button>
      <button class="ef-btn small" type="button" :disabled="loading" @click="reset">清空</button>
    </form>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="query" />

    <template v-else-if="searched && result">
      <div class="pn-pager" style="margin: 0 0 0.9rem">
        <span>会话 <b class="mono">{{ result.conversation_id }}</b></span>
        <span style="margin-left: auto">
          最近 <b>{{ fmtInt(items.length) }}</b> 条带思考过程的消息
        </span>
      </div>

      <StateBlock
        v-if="!items.length"
        kind="empty"
        title="这个会话没有思考记录"
        text="有些模型不返回 reasoning（或者那几条被关掉了）。换一个会话，或者调小/调大条数再试。"
      />

      <div v-else class="pn-reason-list">
        <details v-for="item in items" :key="item.seq" class="pn-reason">
          <summary>
            <span class="seq">#{{ item.seq }}</span>
            <span>{{ item.speaker }}</span>
            <span class="meta">
              {{ fmtDateTime(item.created_at) }} · {{ fmtInt((item.reasoning || '').length) }} 字思考
            </span>
          </summary>
          <div class="body">
            <p class="cap">Reasoning / 思考</p>
            <pre>{{ item.reasoning || '（这条没有思考文本）' }}</pre>
            <p class="cap">Reply / 正式回复（后端只留前 500 字）</p>
            <p class="reply">{{ item.reply || '（空）' }}</p>
          </div>
        </details>
      </div>
    </template>

    <StateBlock
      v-else-if="!searched"
      kind="empty"
      title="先填一个会话 id"
      text="这里不做列表：必须拿具体会话的 public_id 来查，查到的就是从旧到新的最近若干条思考。"
    />
  </div>
</template>
