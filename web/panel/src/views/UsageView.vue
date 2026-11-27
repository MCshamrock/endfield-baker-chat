<script setup lang="ts">
/**
 * UsageView.vue —— 用量（7 / 30 / 90 天）。
 *
 * 这一页是"查账"用的，所以除了直方图还给一张按天的明细表：图上看出哪天有尖峰，
 * 表里查那天的请求数和 token。两者共用同一份数据，不会出现"图上 5 次、表里 3 次"。
 *
 * 时间口径：日期是后端按东八区切的（UTC 时间 +8 小时再 `date()`），前端只补零、不换算。
 * 窗口的右端用**服务器返回的 now**，不用浏览器时钟 —— 免得客户端表慢一天，
 * 最后一天永远是空的。
 */
import { computed, onMounted, ref } from 'vue'
import BarChart, { type BarItem } from '../components/BarChart.vue'
import StateBlock from '../components/StateBlock.vue'
import { api, ApiError } from '../lib/api.ts'
import { fmtDateTime, fmtInt, fmtMs, fmtToken, lastStatsDays } from '../lib/format.ts'
import type { UsageDay } from '../lib/types.ts'

const windowDays = ref<7 | 30 | 90>(7)
const items = ref<UsageDay[]>([])
const serverNow = ref<string | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    // overview 只为拿服务器时间（算窗口右端）；用量本身来自 /admin/usage
    const [usage, overview] = await Promise.all([api.usage(windowDays.value), api.overview()])
    items.value = usage.items
    serverNow.value = overview.now
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '用量加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function pick(days: 7 | 30 | 90) {
  if (windowDays.value === days) return
  windowDays.value = days
  void load()
}

/** 补零后的整段窗口：直方图要 N 根柱子，后端只回有量的天 */
const bars = computed<BarItem[]>(() => {
  const byDate = new Map(items.value.map((d) => [d.date, d]))
  return lastStatsDays(serverNow.value, windowDays.value).map((day) => {
    const hit = byDate.get(day)
    return {
      key: day,
      label: day.slice(5).replace('-', '.'),
      value: hit?.requests ?? 0,
      tip: `${day} · ${hit?.requests ?? 0} 次 · 输入 ${fmtInt(hit?.prompt_tokens ?? 0)} / 输出 ${fmtInt(hit?.completion_tokens ?? 0)} token`,
    }
  })
})

/** 明细只列有量的天，倒序（最近的在上）；全空时表格不渲染 */
const rowsWithData = computed(() => [...items.value].sort((a, b) => (a.date < b.date ? 1 : -1)))
const totals = computed(() =>
  items.value.reduce(
    (acc, d) => ({
      requests: acc.requests + d.requests,
      prompt_tokens: acc.prompt_tokens + d.prompt_tokens,
      completion_tokens: acc.completion_tokens + d.completion_tokens,
      cached_tokens: acc.cached_tokens + d.cached_tokens,
    }),
    { requests: 0, prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0 },
  ),
)
/** 日均：分母是窗口天数（30 天里只有 2 天有量，日均也该按 30 天算） */
const perDay = computed(() => (totals.value.requests / windowDays.value).toFixed(1))
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>用量 <small>Usage</small></h1>
      <p>按天汇总的请求量与 token。窗口右端取服务器时间，日期按东八区（UTC+8）切。</p>
    </header>

    <div class="pn-toolbar">
      <div class="pn-seg">
        <div class="ef-seg" role="radiogroup" aria-label="统计窗口">
          <label v-for="d in [7, 30, 90] as const" :key="d">
            <input type="radio" name="usage-window" :value="d" :checked="windowDays === d" @change="pick(d)" />
            <span>{{ d }} 天</span>
          </label>
        </div>
      </div>
      <button class="ef-btn small push" type="button" @click="load">刷新</button>
    </div>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />

    <template v-else>
      <section class="ef-panel pn-panel">
        <h2>
          近 {{ windowDays }} 天请求量
          <span class="sub">合计 {{ fmtInt(totals.requests) }} 次 · 日均 {{ perDay }} 次</span>
        </h2>
        <BarChart :items="bars" :dense="windowDays > 7" />
        <p class="pn-note">
          {{ windowDays }} 天里只有 <b>{{ rowsWithData.length }}</b> 天有请求。
          {{ windowDays > 7 ? '标签每 5 天标一个，悬停每根柱子看当天读数。' : '' }}
        </p>
      </section>

      <StateBlock
        v-if="!rowsWithData.length"
        kind="empty"
        class="pn-mt"
        title="这段时间没有请求"
        text="只要有人聊天，这里就会出现按天的请求数和 token 消耗。"
      />

      <div v-else class="pn-table-wrap pn-mt">
        <table class="ef-table pn-table">
          <thead>
            <tr>
              <th>日期（UTC+8）</th>
              <th class="num">请求</th>
              <th class="num">输入 token</th>
              <th class="num">输出 token</th>
              <th class="num">命中缓存</th>
              <th class="num">平均耗时</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rowsWithData" :key="row.date">
              <td>{{ row.date }}</td>
              <td class="num">{{ fmtInt(row.requests) }}</td>
              <td class="num">{{ fmtToken(row.prompt_tokens) }}</td>
              <td class="num">{{ fmtToken(row.completion_tokens) }}</td>
              <td class="num">{{ fmtToken(row.cached_tokens) }}</td>
              <td class="num">{{ fmtMs(row.avg_ms) }}</td>
            </tr>
          </tbody>
          <tfoot>
            <tr>
              <th>合计</th>
              <th class="num">{{ fmtInt(totals.requests) }}</th>
              <th class="num">{{ fmtToken(totals.prompt_tokens) }}</th>
              <th class="num">{{ fmtToken(totals.completion_tokens) }}</th>
              <th class="num">{{ fmtToken(totals.cached_tokens) }}</th>
              <th class="num">—</th>
            </tr>
          </tfoot>
        </table>
      </div>

      <p class="pn-note pn-mt">
        表里只列有用量的日子（后端本来也只回这些）。"平均耗时"是当天所有请求的平均，
        合计行没有意义所以留空。服务器时间 {{ fmtDateTime(serverNow) }}。
      </p>
    </template>
  </div>
</template>
