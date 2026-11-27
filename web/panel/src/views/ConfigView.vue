<script setup lang="ts">
/**
 * ConfigView.vue —— 热改配置（安全协议、世界观、各种开关）。
 *
 * 这些值会**直接进模型上下文**，所以这一页的原则是：改之前看得清原文，
 * 改之后说得清"现在生效了没有"。后端 `PUT /v1/admin/config/{key}` 写完会清缓存
 * （见 api/admin.py 的注释），所以保存成功 = 已经生效，不需要重启、不需要等 60 秒。
 *
 * 长文本一律 textarea，等宽字体：安全协议那种一条两百字带编号的东西，
 * 用普通输入框根本没法改。
 */
import { computed, onMounted, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { api, ApiError } from '../lib/api.ts'
import { fmtDateTime } from '../lib/format.ts'
import type { ConfigItem } from '../lib/types.ts'

const items = ref<ConfigItem[]>([])
const loading = ref(true)
const error = ref('')
const busyKey = ref('')
/** 每条的编辑草稿；`savedKey` 用来在保存成功后就地显示"已生效" */
const drafts = ref<Record<string, string>>({})
const savedKey = ref('')
/** 只展开编辑的那几条，其余折叠显示前两行 —— 一条安全协议几百字，全展开滚不动 */
const openKeys = ref<Record<string, boolean>>({})

const dirty = (item: ConfigItem) => (drafts.value[item.key] ?? item.value) !== item.value

const sorted = computed(() => items.value)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const out = await api.listConfig()
    items.value = out.items
    drafts.value = Object.fromEntries(out.items.map((i) => [i.key, i.value]))
    savedKey.value = ''
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '配置加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function save(item: ConfigItem) {
  busyKey.value = item.key
  try {
    const out = await api.putConfig(item.key, drafts.value[item.key] ?? '')
    // 用后端回的值覆盖本地：那是真正落库的内容（含 updated_at）
    item.value = out.value
    item.updated_at = out.updated_at
    drafts.value[item.key] = out.value
    savedKey.value = item.key
    // 三秒后把"已生效"的标记收掉，避免让人以为还在保存
    window.setTimeout(() => {
      if (savedKey.value === item.key) savedKey.value = ''
    }, 3000)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '保存失败'
  } finally {
    busyKey.value = ''
  }
}

function toggleOpen(key: string) {
  openKeys.value[key] = !openKeys.value[key]
}

function reset(item: ConfigItem) {
  drafts.value[item.key] = item.value
}
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>配置 <small>Config</small></h1>
      <p>会进模型上下文的文本。保存即刻生效（后端写完会清缓存）。</p>
    </header>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />
    <StateBlock
      v-else-if="!sorted.length"
      kind="empty"
      title="还没有可改的配置"
      text="安全协议、世界观这些文本在后端初始化时会写进 config 表，之后就会列在这里。"
    />

    <template v-else>
      <p class="ef-notice" data-flag="小心">
        这些文本每位玩家都会读到。改坏了不用慌：把原文粘回来再保存一次就恢复，但中间这段时间
        聊天会按新的文本回答。
      </p>

      <div class="pn-config-list">
        <section v-for="item in sorted" :key="item.key" class="pn-config-row">
          <header>
            <b>{{ item.key }}</b>
            <span class="meta">
              {{ fmtDateTime(item.updated_at) }} · {{ (drafts[item.key] ?? item.value).length }} 字
            </span>
          </header>

          <textarea
            v-if="openKeys[item.key]"
            v-model="drafts[item.key]"
            class="pn-textarea"
            spellcheck="false"
            :disabled="busyKey === item.key"
          />
          <p v-else class="pn-note" style="white-space: pre-wrap; max-height: 5.6rem; overflow: hidden">
            {{ (drafts[item.key] ?? item.value).slice(0, 220) }}{{ (drafts[item.key] ?? item.value).length > 220 ? '…' : '' }}
          </p>

          <div class="pn-config-acts">
            <button class="ef-btn small" type="button" @click="toggleOpen(item.key)">
              {{ openKeys[item.key] ? '收起' : '展开编辑' }}
            </button>
            <button
              class="ef-btn small go"
              type="button"
              :disabled="!dirty(item) || busyKey === item.key"
              :aria-busy="busyKey === item.key"
              @click="save(item)"
            >
              保存
            </button>
            <button class="ef-btn small" type="button" :disabled="!dirty(item)" @click="reset(item)">
              撤销修改
            </button>
            <span v-if="savedKey === item.key" class="saved">已生效（缓存已清）</span>
            <span v-else-if="dirty(item)" class="hint">有未保存的修改</span>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>
