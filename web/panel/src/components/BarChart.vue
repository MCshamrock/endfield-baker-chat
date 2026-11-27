<script setup lang="ts">
/**
 * BarChart.vue —— 按天的请求量直方图。
 *
 * 为什么自己画：只要一排柱子，引一个图表库（echarts 之类）要几百 KB，
 * 而且它的默认配色一定违反这套风格的规则（渐变、圆角、红绿）。
 * 这里就是套件里的 `.ef-bars`：底部一条墨线、柱子用 `--v` 给高度、
 * 悬停变色（`.is-here` 用强调黄）。**柱子高度、标签、读数全是真数据。**
 */
import { computed } from 'vue'

export interface BarItem {
  /** 唯一键（日期串） */
  key: string
  /** 轴上的短标签 */
  label: string
  /** 这一格的量 */
  value: number
  /** 悬停时的完整说明 */
  tip: string
}

const props = defineProps<{
  items: BarItem[]
  /** 30/90 天：柱子很细，标签只留隔几个的 */
  dense?: boolean
}>()

/** 最高的一根决定 100%；全是 0 时用 1 兜底，别除出 NaN */
const max = computed(() => Math.max(1, ...props.items.map((i) => i.value)))

/**
 * 柱高。套件的 `.ef-bars > li` 写的是 `height: calc(var(--v) * 100%)`，
 * 所以 `--v` 必须是**无单位的小数**（0.43），给百分比会被算成 `100% * 100%`，
 * 整条 CSS 规则作废、柱子全部消失（这个坑踩过一次）。
 */
function heightOf(value: number): string {
  if (value <= 0) return '0'
  // 最小 0.04：有量但很小的那天也要看得见，否则会被误读成"没有"
  return String(Math.max(0.04, Math.round((value / max.value) * 100) / 100))
}
</script>

<template>
  <div class="pn-chart" :class="{ 'is-dense': dense }">
    <ul class="ef-bars" :aria-label="`${items.length} 天请求量`">
      <li
        v-for="item in items"
        :key="item.key"
        :style="{ '--v': heightOf(item.value) }"
        :class="{ 'is-zero': item.value === 0 }"
        :title="item.tip"
      >
        <span>{{ item.label }}</span>
      </li>
    </ul>
    <p class="pn-chart-foot">
      <span>峰值 <b>{{ Math.max(...items.map((i) => i.value)) }}</b> 次</span>
      <span>
        平均
        <b>{{ items.length ? Math.round((items.reduce((a, i) => a + i.value, 0) / items.length) * 10) / 10 : 0 }}</b>
        次/天
      </span>
      <span>合计 <b>{{ items.reduce((a, i) => a + i.value, 0) }}</b> 次</span>
      <slot name="foot" />
    </p>
  </div>
</template>
