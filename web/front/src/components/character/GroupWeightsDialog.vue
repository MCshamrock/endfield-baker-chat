<script setup lang="ts">
// =============================================================================
// 发言权重弹窗(GroupWeightsDialog)
// -----------------------------------------------------------------------------
// 建群之后让玩家给每位成员调「发言权重」（1–100）。权重只影响**加权模式**：
// 越高越容易被抽中开口；本地即时生效，服务端存的是这台设备/账号的覆盖值
// （`PUT /v1/characters/{slug}/weight`）。
//
// 为什么建完群才弹：权重是"这群里谁爱抢话"的调节旋钮，成员都没定下来时
// 调它没有意义；而且新建的群多半就是要马上调。
//
// 后端没有"读取我已设过的权重"的端点（GET /characters 只给角色自带默认值），
// 所以显示顺序是：本地缓存 → 服务端默认 → 50。
// =============================================================================
import { computed, ref, watch } from 'vue'
import VersionDialogShell from '../layout/VersionDialogShell.vue'
import { findCharacter } from '../../constants/character'
import {
  fetchDefaultWeights,
  readLocalWeights,
  setCharacterWeight,
} from '../../utils/groupBackend'

const props = defineProps<{
  /** 是否展开 */
  open: boolean
  /** 群成员中文名（顺序即展示顺序） */
  members: string[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /** 保存成功（参数 = 实际写入的成员数） */
  (e: 'saved', count: number): void
}>()

/** 正在拉默认权重 */
const loading = ref(false)
/** 正在保存 */
const saving = ref(false)
/** 错误提示 */
const error = ref('')

/** 角色自带的默认权重（服务端） */
const defaults = ref<Record<string, number>>({})
/** 当前编辑值（角色名 → 1..100） */
const values = ref<Record<string, number>>({})

/** 表头说明 */
const subtitle = computed(() => `共 ${props.members.length} 位成员 · 只影响「加权模式」`)

/** 用「本地覆盖 → 服务端默认 → 50」初始化编辑值 */
function seedValues(): void {
  const local = readLocalWeights()
  const next: Record<string, number> = {}
  for (const name of props.members) {
    next[name] = local[name] ?? defaults.value[name] ?? 50
  }
  values.value = next
}

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    error.value = ''
    loading.value = true
    defaults.value = await fetchDefaultWeights()
    seedValues()
    loading.value = false
  },
  { immediate: true },
)

/** 成员列表在弹窗打开期间变化（理论上不会）时同步一次 */
watch(
  () => props.members.join('|'),
  () => {
    if (props.open && !loading.value) seedValues()
  },
)

/** 头像 */
function avatarOf(name: string): string {
  return findCharacter(name)?.avatar ?? ''
}

/** 权重文字（夹在 1..100） */
function clamp(value: number): number {
  if (!Number.isFinite(value)) return 50
  return Math.max(1, Math.min(100, Math.round(value)))
}

/** 保存全部 */
async function onSave(): Promise<void> {
  if (saving.value) return
  saving.value = true
  error.value = ''
  let ok = 0
  for (const name of props.members) {
    const value = clamp(values.value[name] ?? 50)
    values.value[name] = value
    if (await setCharacterWeight(name, value)) ok += 1
  }
  saving.value = false
  if (ok === props.members.length) {
    emit('saved', ok)
    emit('close')
  } else {
    error.value = `只保存了 ${ok}/${props.members.length} 位成员，请稍后重试`
  }
}
</script>

<template>
  <VersionDialogShell
    :open="open"
    title="发言权重"
    kind="notice"
    :closable="!saving"
    @close="emit('close')"
  >
    <p class="gw__sub">{{ subtitle }}</p>
    <p class="gw__tip">
      数值越高，她在「加权模式」里越容易开口；1 = 几乎不主动，100 = 非常爱说话。
    </p>

    <div v-if="loading" class="gw__loading">正在读取角色权重…</div>

    <div v-else class="gw__list">
      <div v-for="name in members" :key="name" class="gw__row">
        <span class="gw__avatar">
          <img v-if="avatarOf(name)" :src="avatarOf(name)" :alt="name" />
        </span>
        <span class="gw__name">{{ name }}</span>
        <input
          v-model.number="values[name]"
          class="gw__range"
          type="range"
          min="1"
          max="100"
          step="1"
          :aria-label="`${name}的发言权重`"
          :disabled="saving"
        />
        <span class="gw__value">{{ clamp(values[name] ?? 50) }}</span>
      </div>
    </div>

    <p v-if="error" class="gw__error">{{ error }}</p>

    <template #actions>
      <button class="gw__btn gw__btn--primary" type="button" :disabled="saving || loading" @click="onSave">
        {{ saving ? '保存中…' : '保存权重' }}
      </button>
      <button class="gw__btn" type="button" :disabled="saving" @click="emit('close')">跳过</button>
    </template>
  </VersionDialogShell>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

// 正文是白底（VersionDialogShell 的浅色区），文字全部用深色
.gw {
  &__sub {
    margin: 0 0 6px;
    color: #4b5563;
    font-family: $font-harmony;
    font-size: 13px;
  }

  &__tip {
    margin: 0 0 12px;
    color: #6b7280;
    font-family: $font-harmony;
    font-size: 12px;
    line-height: 1.6;
  }

  &__loading {
    padding: 18px 0;
    color: #6b7280;
    font-family: $font-harmony;
    font-size: 13px;
    text-align: center;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 42vh;
    overflow-y: auto;
    padding-right: 4px;
  }

  &__row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 10px;
    border: 1px solid rgba(17, 24, 39, 0.1);
    border-radius: 4px;
    background: #f7f7f7;
  }

  &__avatar {
    flex-shrink: 0;
    width: 30px;
    height: 30px;
    overflow: hidden;
    border-radius: 3px;
    background: #e5e7eb;

    img {
      width: 100%;
      height: 100%;
      object-fit: cover;
    }
  }

  &__name {
    flex-shrink: 0;
    width: 72px;
    overflow: hidden;
    color: #1f2937;
    font-family: $font-harmony;
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__range {
    flex: 1;
    min-width: 0;
    accent-color: #d8cf00;
    cursor: pointer;
  }

  &__value {
    flex-shrink: 0;
    width: 34px;
    color: #111827;
    font-family: $font-harmony;
    font-size: 13px;
    font-weight: 500;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  &__error {
    margin: 10px 0 0;
    color: #b91c1c;
    font-family: $font-harmony;
    font-size: 12px;
  }

  &__btn {
    min-width: 96px;
    padding: 8px 18px;
    border: 1px solid rgba(17, 24, 39, 0.25);
    background: #f3f3f3;
    color: #1f2937;
    font-family: $font-harmony;
    font-size: 13px;
    cursor: pointer;

    &:hover:not(:disabled) {
      background: #e9e9e9;
    }

    &:disabled {
      opacity: 0.55;
      cursor: default;
    }

    &--primary {
      border-color: #d8cf00;
      background: #fcf33f;
      color: #241d00;
      font-weight: 500;

      &:hover:not(:disabled) {
        background: #f7ea00;
      }
    }
  }
}
</style>
