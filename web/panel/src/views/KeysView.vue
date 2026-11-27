<script setup lang="ts">
/**
 * KeysView.vue —— 密钥池（**只读状态 + 启停**）。
 *
 * 三条铁律写在这里，改这一页之前先读：
 *  1. **永远不显示密钥内容**。后端 `GET /v1/admin/keys` 连密文都不返回，
 *     所以这一页根本没有可显示的密钥字段 —— 不是我们把它藏起来了，是它没来过。
 *  2. 禁用一把 key 只表示"下次别再借它"，不会踢掉正在跑的请求（后端注释原话）。
 *  3. 不编数字。这个接口没有"成功率"和"延迟"，所以这里不显示这两列 ——
 *     要看得让后端先统计，而不是在前端拿 fail_count 除一个假总数。
 */
import { computed, onMounted, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { toast } from '../assets/endfield/fx.js'
import { api, ApiError } from '../lib/api.ts'
import { fmtDateTime, fmtInt, fmtPct } from '../lib/format.ts'
import type { AdminKey } from '../lib/types.ts'

const rows = ref<AdminKey[]>([])
const loading = ref(true)
const error = ref('')
const busyId = ref<number | null>(null)
const filter = ref<'all' | 'on' | 'off' | 'cool'>('all')

const stats = computed(() => api.keyStatsOf(rows.value))
const onCount = computed(() => rows.value.filter((r) => r.enabled).length)
const coolCount = computed(() => rows.value.filter((r) => r.enabled && r.cooling).length)

const filtered = computed(() => {
  if (filter.value === 'on') return rows.value.filter((r) => r.enabled)
  if (filter.value === 'off') return rows.value.filter((r) => !r.enabled)
  if (filter.value === 'cool') return rows.value.filter((r) => r.enabled && r.cooling)
  return rows.value
})

/** 一行三态：冷却 > 启用 > 禁用。用套件的状态件：实线=生效、虚线=后台、黄块=要看一眼 */
function stateOf(row: AdminKey): { text: string; cls: string } {
  if (!row.enabled) return { text: '禁用', cls: 'is-hidden' }
  if (row.cooling) return { text: '冷却中', cls: 'is-alert' }
  return { text: '启用', cls: '' }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const out = await api.listKeys()
    rows.value = out.items
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '密钥列表加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

async function toggle(row: AdminKey) {
  busyId.value = row.id
  try {
    const out = await api.patchKey(row.id, !row.enabled)
    row.enabled = out.enabled
    toast(`${row.label} 已${out.enabled ? '启用' : '禁用'}`)
  } catch (err) {
    toast(err instanceof ApiError ? err.message : '改失败', 3000)
  } finally {
    busyId.value = null
  }
}
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>密钥 <small>Key Pool</small></h1>
      <p>只显示状态，不显示密钥。启停只影响"下次借不借它"，不会打断正在跑的请求。</p>
    </header>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />

    <template v-else>
      <div class="pn-grid cols-2">
        <section class="ef-panel pn-panel">
          <h2>池子 <span class="sub">启用 {{ stats.enabled }} / {{ stats.total }}</span></h2>
          <div class="pn-figs">
            <div><b>{{ fmtInt(stats.total) }}</b><span class="label">总数</span></div>
            <div><b>{{ fmtInt(stats.enabled) }}</b><span class="label">启用</span></div>
            <div :class="{ 'is-alert': stats.cooldown > 0 }"><b>{{ fmtInt(stats.cooldown) }}</b><span class="label">冷却中</span></div>
            <div><b>{{ fmtInt(stats.disabled) }}</b><span class="label">禁用</span></div>
          </div>
          <ul class="pn-ledger">
            <li><span>启用比例</span><b>{{ fmtPct(stats.enabled, stats.total) }}</b></li>
            <li><span>冷却中的占比（对着启用数算）</span><b>{{ fmtPct(coolCount, onCount) }}</b></li>
          </ul>
        </section>

        <section class="ef-panel pn-panel">
          <h2>这一页没有什么 <span class="sub">口径</span></h2>
          <ul class="pn-ledger">
            <li><span>密钥内容（明文 / 密文）</span><b>接口不返回</b></li>
            <li><span>成功率、平均延迟</span><b>后端未统计</b></li>
          </ul>
          <p class="pn-note">
            前者是设计（`api/repos/admin.py` 规定明文不出服务端）；后者要加统计才有，
            这里不拿失败次数凑一个假成功率。
          </p>
        </section>
      </div>

      <div class="pn-toolbar pn-mt">
        <div class="pn-tag-list">
          <button class="ef-chip" type="button" :aria-pressed="filter === 'all'" @click="filter = 'all'">
            全部 <span class="n">{{ rows.length }}</span>
          </button>
          <button class="ef-chip" type="button" :aria-pressed="filter === 'on'" @click="filter = 'on'">
            启用 <span class="n">{{ onCount }}</span>
          </button>
          <button class="ef-chip" type="button" :aria-pressed="filter === 'cool'" @click="filter = 'cool'">
            冷却 <span class="n">{{ coolCount }}</span>
          </button>
          <button class="ef-chip" type="button" :aria-pressed="filter === 'off'" @click="filter = 'off'">
            禁用 <span class="n">{{ stats.disabled }}</span>
          </button>
        </div>
        <button class="ef-btn small push" type="button" @click="load">刷新</button>
      </div>

      <StateBlock v-if="!filtered.length" kind="empty" title="这个筛选下没有 key" text="换个筛选，或者等下一轮把新 key 导进去。" />

      <div v-else class="pn-table-wrap">
        <table class="ef-table pn-table">
          <thead>
            <tr>
              <th class="narrow">ID</th>
              <th class="narrow">标记</th>
              <th class="narrow">状态</th>
              <th class="narrow">冷却至</th>
              <th class="num narrow">失败次数</th>
              <th class="num narrow">RPM</th>
              <th>模型 / 上游</th>
              <th class="narrow">上次使用</th>
              <th class="narrow">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in filtered" :key="row.id" :class="{ 'is-off': !row.enabled }">
              <td class="narrow mono">{{ row.id }}</td>
              <td class="narrow">{{ row.label }}</td>
              <td class="narrow">
                <span class="ef-state" :class="stateOf(row).cls">{{ stateOf(row).text }}</span>
              </td>
              <td class="narrow">{{ fmtDateTime(row.cooldown_until) }}</td>
              <td class="num narrow">
                <b v-if="row.fail_count > 0" class="ef-state is-alert">{{ row.fail_count }}</b>
                <span v-else>0</span>
              </td>
              <td class="num narrow">{{ row.rpm }}</td>
              <td class="mono" style="max-width: 22rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                {{ row.model || '—' }} │ {{ row.base_url }}
              </td>
              <td class="narrow">{{ fmtDateTime(row.last_used_at) }}</td>
              <td class="narrow">
                <button class="ef-btn small" type="button" :disabled="busyId === row.id" @click="toggle(row)">
                  {{ row.enabled ? '禁用' : '启用' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <p class="pn-note pn-mt">
        冷却中的 key 到点会自动恢复接活，不用手工点。<b>禁用</b>才是长期停用；停用后它不计入可用池。
      </p>
    </template>
  </div>
</template>
