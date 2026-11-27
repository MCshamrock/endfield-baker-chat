<script setup lang="ts">
/**
 * OverviewView.vue —— 总览。
 *
 * 这一屏回答四个问题，顺序就是老板打开后台的顺序：
 *   1. 站里现在有多少东西（用户 / 设备 / 会话 / 消息 / 干员）
 *   2. 密钥池还够不够用（总数 / 启用 / 冷却 / 禁用）
 *   3. 今天跑了多少（请求数 / token / 缓存命中 / 平均耗时）
 *   4. 最近 7 天有没有掉下来（直方图）
 *
 * 所有数字都来自 `GET /v1/admin/overview` 一次请求，不额外打接口 ——
 * 首页慢一下能忍，首页打五个接口就不能忍了。
 */
import { computed, onMounted, ref } from 'vue'
import BarChart, { type BarItem } from '../components/BarChart.vue'
import StateBlock from '../components/StateBlock.vue'
import { api, ApiError } from '../lib/api.ts'
import { fmtDateTime, fmtInt, fmtMs, fmtPct, fmtToken, lastStatsDays } from '../lib/format.ts'
import type { Overview } from '../lib/types.ts'

const data = ref<Overview | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.overview()
  } catch (err) {
    // 401 已经由全局处理器送回登录页，这里只负责"其它错"的显示
    error.value = err instanceof ApiError ? err.message : '总览加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** 近 7 天：后端只回"有请求"的那几天，缺的日子在这里补 0。
 *  日期全部按东八区日对齐（后端 `date(created_at + 8h)` 的口径），见页面底部的说明。 */
const weekBars = computed<BarItem[]>(() => {
  if (!data.value) return []
  const byDate = new Map(data.value.days.map((d) => [d.date, d]))
  return lastStatsDays(data.value.now, 7).map((day) => {
    const hit = byDate.get(day)
    return {
      key: day,
      label: day.slice(5).replace('-', '.'),
      value: hit?.requests ?? 0,
      tip: `${day} · ${hit?.requests ?? 0} 次请求 · 输出 ${fmtToken(hit?.completion_tokens ?? 0)} token`,
    }
  })
})

const today = computed(() => data.value?.today)
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>总览 <small>Overview</small></h1>
      <p>全站的规模和今天的用量。数字每次进这一页重新拉一次。</p>
    </header>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />

    <template v-else-if="data">
      <div class="pn-grid overview-top">
        <!-- 规模 -->
        <section class="ef-panel pn-panel">
          <h2>规模 <span class="sub">全站累计</span></h2>
          <div class="pn-figs">
            <div>
              <b>{{ fmtInt(data.counts.users) }}</b>
              <span class="label">用户</span>
            </div>
            <div>
              <b>{{ fmtInt(data.counts.devices) }}</b>
              <span class="label">设备</span>
            </div>
            <div>
              <b>{{ fmtInt(data.counts.conversations) }}</b>
              <span class="label">会话</span>
            </div>
            <div>
              <b>{{ fmtInt(data.counts.messages) }}</b>
              <span class="label">消息</span>
            </div>
            <div>
              <b>{{ fmtInt(data.counts.characters) }}</b>
              <span class="label">干员</span>
            </div>
          </div>
          <ul class="pn-ledger">
            <li><span>世界书条目</span><b>{{ fmtInt(data.counts.worldbook) }}</b></li>
            <li>
              <span>平均每条会话的消息数</span>
              <b>{{ data.counts.conversations ? (data.counts.messages / data.counts.conversations).toFixed(1) : '—' }}</b>
            </li>
          </ul>
        </section>

        <!-- 密钥池 -->
        <section class="ef-panel pn-panel">
          <h2>
            密钥池 <span class="sub">启用 {{ data.keys.enabled }} / {{ data.keys.total }}</span>
          </h2>
          <div class="pn-figs">
            <div>
              <b>{{ fmtInt(data.keys.total) }}</b>
              <span class="label">总数</span>
            </div>
            <div>
              <b>{{ fmtInt(data.keys.enabled) }}</b>
              <span class="label">启用</span>
            </div>
            <div :class="{ 'is-alert': data.keys.cooldown > 0 }">
              <b>{{ fmtInt(data.keys.cooldown) }}</b>
              <span class="label">冷却中</span>
            </div>
            <div>
              <b>{{ fmtInt(data.keys.disabled) }}</b>
              <span class="label">禁用</span>
            </div>
          </div>
          <p class="pn-note">
            启用的比例 {{ fmtPct(data.keys.enabled, data.keys.total) }}。冷却中的 key 只是暂时不接活，
            禁用的要手工开（见「密钥」页）。
          </p>
        </section>
      </div>

      <div class="pn-grid overview-bottom pn-mt">
        <!-- 近 7 天 -->
        <section class="ef-panel pn-panel">
          <h2>近 7 天请求量 <span class="sub">按天 · 东八区日界</span></h2>
          <BarChart :items="weekBars" />
          <p class="pn-note">
            没有请求的那天画成一条浅色底线（格子还在，只是没量）。数字鼠标悬停可看。
          </p>
        </section>

        <!-- 今日 -->
        <section class="ef-panel pn-panel">
          <h2>今日用量 <span class="sub">东八区零点起</span></h2>
          <div class="pn-figs">
            <div>
              <b>{{ fmtInt(today?.requests) }}</b>
              <span class="label">请求</span>
            </div>
            <div>
              <b>{{ fmtToken(today?.completion_tokens) }}</b>
              <span class="label">输出 token</span>
            </div>
          </div>
          <ul class="pn-ledger">
            <li><span>输入 token</span><b>{{ fmtToken(today?.prompt_tokens) }}</b></li>
            <li><span>命中缓存 token</span><b>{{ fmtToken(today?.cached_tokens) }}</b></li>
            <li><span>平均耗时</span><b>{{ fmtMs(today?.avg_ms) }}</b></li>
          </ul>
          <p class="pn-note">
            缓存命中率 {{ fmtPct(today?.cached_tokens ?? 0, today?.prompt_tokens ?? 0) }} ——
            命中是省钱的那部分输入。
          </p>
        </section>
      </div>

      <p class="pn-note pn-mt">
        服务器时间 {{ fmtDateTime(data.now) }}。
        「今日」和直方图的日子都按东八区（UTC+8）切天：库里的时间存的是 UTC，
        后端 +8 小时后再取日期，所以和你在东八区看到的日历一致。
      </p>
    </template>
  </div>
</template>
