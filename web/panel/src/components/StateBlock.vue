<script setup lang="ts">
/**
 * StateBlock.vue —— 三种"没有内容"的样子：加载中 / 空的 / 出错了。
 *
 * 为什么单独一个组件：8 个页面都要这三态，散着写一定会有人漏掉"失败态"，
 * 然后那一页在后端挂掉时变成一片白 —— 白屏是最难排查的故障。
 * 这里强制每个页面至少用一次它。
 */
defineProps<{
  kind: 'loading' | 'empty' | 'error'
  /** 空状态/错误的一句话。空状态写"这里将来会出现什么"，不写"暂无数据"。 */
  text?: string
  /** 空状态的粗体标题（可选） */
  title?: string
}>()

const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="kind === 'loading'" class="pn-skeleton" aria-busy="true" aria-label="正在加载">
    <i />
    <i />
    <i />
    <i />
  </div>

  <div v-else-if="kind === 'empty'" class="pn-empty">
    <b>{{ title || '这里还没有内容' }}</b>
    <span>{{ text }}</span>
  </div>

  <p v-else class="ef-notice" data-flag="出错">
    <span>{{ text || '请求失败，稍后再试' }}</span>
    <button class="ef-btn small" type="button" style="margin-left: auto" @click="emit('retry')">
      重试
    </button>
  </p>
</template>
