<script setup lang="ts">
/**
 * CharactersView.vue —— 干员（角色）管理。
 *
 * 这一页只有两个高频动作：**上下架**和**调发言权重**（权重决定群里谁更爱说话）。
 * 两个都要求"点一下就生效、立刻能看到结果"，所以放在行内，改完把后端回的值
 * 合并进这一行，不整表重刷（重刷会让滚动位置跳、也看不清哪行变了）。
 *
 * 一个后端现状要说明：`GET /v1/admin/characters` 现在会返回**重复行**
 * （character_prompts 里每个干员有两条 active=1 的版本，join 之后一行变两行）。
 * 前端按 slug 去重、版本号取最大的那个，并在页面上把这件事标出来 ——
 * 宁可在界面上写清楚，也不要让 36 个干员在表里变成 72 行。
 */
import { computed, onMounted, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { toast } from '../assets/endfield/fx.js'
import { api, ApiError } from '../lib/api.ts'
import type { AdminCharacter } from '../lib/types.ts'

type Draft = { weight: string; sort: string }

const rows = ref<AdminCharacter[]>([])
/** 后端返回的原始行数（去重前），用来说明"为什么去重" */
const rawCount = ref(0)
const loading = ref(true)
const error = ref('')
const busySlug = ref('')
const drafts = ref<Record<string, Draft>>({})
const filter = ref<'all' | 'on' | 'off'>('all')

const filtered = computed(() => {
  if (filter.value === 'on') return rows.value.filter((r) => r.enabled)
  if (filter.value === 'off') return rows.value.filter((r) => !r.enabled)
  return rows.value
})

const onCount = computed(() => rows.value.filter((r) => r.enabled).length)
const offCount = computed(() => rows.value.length - onCount.value)

function resetDrafts(list: AdminCharacter[]) {
  drafts.value = Object.fromEntries(
    list.map((r) => [r.id, { weight: String(r.active_weight), sort: String(r.sort_order) }]),
  )
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const out = await api.listCharacters()
    rawCount.value = out.items.length
    // 去重：同一个 slug 取 prompt_ver 最大的那行（版本号代表"当前提示词"）
    const bySlug = new Map<string, AdminCharacter>()
    for (const item of out.items) {
      const seen = bySlug.get(item.id)
      if (!seen || item.prompt_ver > seen.prompt_ver) bySlug.set(item.id, item)
    }
    rows.value = [...bySlug.values()].sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name))
    resetDrafts(rows.value)
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '干员列表加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

const dirty = (row: AdminCharacter) => {
  const d = drafts.value[row.id]
  return !!d && (Number(d.weight) !== row.active_weight || Number(d.sort) !== row.sort_order)
}

async function save(row: AdminCharacter) {
  const d = drafts.value[row.id]
  if (!d) return
  const weight = Math.round(Number(d.weight))
  const sort = Math.round(Number(d.sort))
  if (!Number.isFinite(weight) || weight < 1 || weight > 100) {
    toast('发言权重只接受 1–100 的整数', 3000)
    return
  }
  if (!Number.isFinite(sort)) {
    toast('排序要是整数', 2600)
    return
  }
  busySlug.value = row.id
  try {
    const out = await api.patchCharacter(row.id, { active_weight: weight, sort_order: sort })
    row.active_weight = out.active_weight
    row.sort_order = out.sort_order
    d.weight = String(out.active_weight)
    d.sort = String(out.sort_order)
    toast(`${row.name} 权重 ${out.active_weight} · 排序 ${out.sort_order}`)
  } catch (err) {
    toast(err instanceof ApiError ? err.message : '保存失败', 3000)
    await load()
  } finally {
    busySlug.value = ''
  }
}

async function toggle(row: AdminCharacter) {
  busySlug.value = row.id
  try {
    const out = await api.patchCharacter(row.id, { enabled: !row.enabled })
    row.enabled = out.enabled
    toast(`${row.name} 已${out.enabled ? '上架' : '下架'}`)
  } catch (err) {
    toast(err instanceof ApiError ? err.message : '改上下架失败', 3000)
  } finally {
    busySlug.value = ''
  }
}
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>干员 <small>Operators</small></h1>
      <p>上下架、发言权重（1–100）、排序。权重越大，在群聊里越常开口。</p>
    </header>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />

    <template v-else>
      <div class="pn-toolbar">
        <div class="pn-tag-list">
          <button class="ef-chip" type="button" :aria-pressed="filter === 'all'" @click="filter = 'all'">
            全部 <span class="n">{{ rows.length }}</span>
          </button>
          <button class="ef-chip" type="button" :aria-pressed="filter === 'on'" @click="filter = 'on'">
            已上架 <span class="n">{{ onCount }}</span>
          </button>
          <button class="ef-chip" type="button" :aria-pressed="filter === 'off'" @click="filter = 'off'">
            已下架 <span class="n">{{ offCount }}</span>
          </button>
        </div>
        <button class="ef-btn small push" type="button" @click="load">刷新</button>
      </div>

      <StateBlock
        v-if="!filtered.length"
        kind="empty"
        :title="filter === 'off' ? '没有下架的干员' : filter === 'on' ? '一个都没上架' : '还没有干员'"
        text="干员随提示词一起导入；导入之后会出现在这张表里。"
      />

      <div v-else class="pn-table-wrap">
        <table class="ef-table pn-table">
          <thead>
            <tr>
              <th class="narrow">排序</th>
              <th>干员</th>
              <th class="narrow">上架</th>
              <th class="num narrow">发言权重</th>
              <th class="num narrow">显示顺序</th>
              <th class="num narrow">提示词</th>
              <th class="narrow">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in filtered" :key="row.id" :class="{ 'is-off': !row.enabled }">
              <td class="narrow mono">{{ row.sort_order }}</td>
              <td>
                <b>{{ row.name }}</b>
                <div class="mono">{{ row.id }}<template v-if="row.attribute"> │ {{ row.attribute }}</template></div>
              </td>
              <td class="narrow">
                <span class="ef-state" :class="{ 'is-hidden': !row.enabled }">{{ row.enabled ? '上架' : '下架' }}</span>
              </td>
              <td class="num narrow">
                <input
                  v-model="drafts[row.id].weight"
                  class="pn-input tiny"
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  :disabled="busySlug === row.id"
                  @keydown.enter.prevent="save(row)"
                />
              </td>
              <td class="num narrow">
                <input
                  v-model="drafts[row.id].sort"
                  class="pn-input tiny"
                  type="number"
                  step="1"
                  :disabled="busySlug === row.id"
                  @keydown.enter.prevent="save(row)"
                />
              </td>
              <td class="num narrow">
                <span v-if="row.prompt_ver">v{{ row.prompt_ver }}</span>
                <span v-else class="ef-state is-alert">缺</span>
              </td>
              <td class="narrow">
                <div class="pn-actions">
                  <button
                    class="ef-btn small"
                    type="button"
                    :disabled="!dirty(row) || busySlug === row.id"
                    :aria-busy="busySlug === row.id"
                    @click="save(row)"
                  >
                    保存
                  </button>
                  <button
                    class="ef-btn small"
                    type="button"
                    :disabled="busySlug === row.id"
                    @click="toggle(row)"
                  >
                    {{ row.enabled ? '下架' : '上架' }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="pn-note pn-mt">
        下架的干员保留数据，只是不再出现在客户端的可选列表里。提示词版本 <b>v0 / 缺</b> 表示这个干员
        还没有启用中的提示词，客户端就算上架他也说不了话。
      </p>
      <p v-if="rawCount > rows.length" class="ef-notice pn-mt" data-flag="数据">
        后端返回 {{ rawCount }} 行、实际 {{ rows.length }} 个干员（每个干员有两条 active 提示词，
        join 后重复）。这里按 slug 去重、版本取最大的那条显示；要根治得改后端。
      </p>
    </template>
  </div>
</template>
