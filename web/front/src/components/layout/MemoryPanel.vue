<script setup lang="ts">
// =============================================================================
// 干员记忆面板(MemoryPanel)
// -----------------------------------------------------------------------------
// 「跨对话记忆」的唯一前端入口 —— 挂在 设置 → 干员记忆。
//
// 三块，对应 docs/04-干员记忆.md 的两层开关 + 一个面板：
//
//   1. 全局开关（GET/PUT /v1/memories/settings）
//      「关掉后不再跨对话记关于你的事；已记下的仍可查看、可单独删」。
//      关的只是**读取/写入的默认值**，账本一条都不会没 —— 所以列表照旧能看能删。
//
//   2. 她记得什么（GET /v1/memories?character_id=… × 每个干员）
//      按干员分组；每条显示 kind 中文标签 + 内容 + 时间，支持删除（二次确认）。
//      后端没有「一次拿全部干员」的端点，所以这里是「先拉角色目录，再逐干员查」，
//      只在用户点开列表时查一次（见 utils/memoryApi.ts 的说明）。
//
//   3. 新建对话时的本局记忆（本地偏好，建会话时才用得上）
//      ⚠ 后端**没有改已有会话的接口**：memory_mode 只在 POST /v1/conversations
//      建会话时写进去。所以这里只能定"下一局"，已经开着的对话改不了 ——
//      界面上必须把这条限制说清，否则看起来像"点了没反应"。
//
// 状态：加载中 / 空态 / 失败（可重试）/ 保存失败（回滚 UI + 提示）都画了。
// =============================================================================
import { onMounted, ref, computed } from 'vue'
import SetSection from './ui/SetSection.vue'
import SetRow from './ui/SetRow.vue'
import SegControl from './ui/SegControl.vue'
import type { SegOption } from './ui/SegControl.vue'
import { findCharacter } from '../../constants/character'
import { friendlyError } from '../../utils/apiClient'
import {
  deleteMemory,
  fetchAllMemories,
  fetchMemorySettings,
  formatMemoryTime,
  memoryKindLabel,
  readNewChatMemory,
  saveMemorySettings,
  writeNewChatMemory,
  type MemoryGroup,
  type MemoryItem,
  type MemoryMode,
  type MemoryNewChat,
} from '../../utils/memoryApi'

// ---- 全局开关 -----------------------------------------------------------------
/** 当前全局开关（乐观更新：点下去先变，失败再回滚） */
const mode = ref<MemoryMode>('on')
/** 首次读取中（此时开关禁用，避免"还没读到就点"） */
const modeLoading = ref(true)
/** 正在保存开关 */
const modeSaving = ref(false)
/**
 * 读取开关失败（GET 挂了）—— 这种情况下"重试"才有意义，
 * 所以单独一个 ref：保存失败已经回滚了 UI，不该再提示"重新读取设置"。
 */
const modeLoadError = ref('')
/** 保存开关失败（PUT 挂了，UI 已回滚） */
const modeSaveError = ref('')

/** 开关的分段选项（后端只有 on/off 两档，别自造第三档） */
const MODE_SEG: SegOption[] = [
  { value: 'on', label: '开' },
  { value: 'off', label: '关' },
]

/** 开关下方那句状态说明（跟着真实值走，不跟点击走） */
const modeHint = computed(() => {
  if (modeLoading.value) return '正在读取设置…'
  if (modeSaving.value) return '正在保存…'
  if (modeLoadError.value) return modeLoadError.value
  if (modeSaveError.value) return modeSaveError.value
  return mode.value === 'on'
    ? '已开启：换一个对话，她仍然记得你是谁'
    : '已关闭：不再跨对话记新的；已记下的都还在，可以继续查看和删除'
})

/** 状态说明的语气（错误用 warn，让玩家一眼看到"没保存上"） */
const modeTone = computed<'ok' | 'warn' | 'keep'>(() => {
  if (modeLoading.value || modeSaving.value) return 'keep'
  if (modeLoadError.value || modeSaveError.value) return 'warn'
  return mode.value === 'on' ? 'ok' : 'warn'
})

/** 读一次全局开关（面板每次挂载 / 读失败重试都走它） */
async function loadMode(): Promise<void> {
  modeLoading.value = true
  modeLoadError.value = ''
  try {
    mode.value = await fetchMemorySettings()
  } catch (err) {
    modeLoadError.value = `读取设置失败：${friendlyError(err)}`
  } finally {
    modeLoading.value = false
  }
}

/**
 * 切开关
 *
 * 先改 UI（乐观更新）再发请求：这个开关点下去要立刻有反馈，
 * 等一个来回才动会让人以为没点上。失败则**回滚**并写明原因，
 * 绝不能让界面停在一个后端其实没接受的状态。
 */
async function onToggleMode(value: string): Promise<void> {
  const want: MemoryMode = value === 'off' ? 'off' : 'on'
  if (modeSaving.value || want === mode.value) return

  const previous = mode.value
  mode.value = want
  modeSaving.value = true
  modeLoadError.value = ''
  modeSaveError.value = ''
  try {
    mode.value = await saveMemorySettings(want)
    // 列表顶部那句"记忆已关闭"的提示跟着 mode 自动变；账本本身不动
  } catch (err) {
    mode.value = previous
    modeSaveError.value = `保存失败，已恢复原设置：${friendlyError(err)}`
  } finally {
    modeSaving.value = false
  }
}

// ---- 她记得什么（列表） -------------------------------------------------------
/** 列表是否展开（默认收起：摊开 36 个干员的查询，不该在打开设置时就跑） */
const expanded = ref(false)
/** 列表加载中 */
const listLoading = ref(false)
/** 列表是否已经成功加载过（失败后重试、避免每次展开都重查） */
const listLoaded = ref(false)
/** 列表错误 */
const listError = ref('')
/** 按干员分组的结果 */
const groups = ref<MemoryGroup[]>([])
/** 条目总数 */
const total = ref(0)
/** 个别干员没读到的数量（网络抖动；>0 时给一句提示，不整块报错） */
const partialFailures = ref(0)

/** 「展开 / 收起」按钮文案 */
const toggleLabel = computed(() => {
  if (!expanded.value) return '展开'
  return listLoading.value ? '读取中…' : '收起'
})

/** 列表上方那一行汇总（收起时也给个概览，不用点开才知道有几条） */
const listSummary = computed(() => {
  if (!listLoaded.value) return '按干员分组；点开才读取'
  if (total.value === 0) return '她还没记住你的事'
  return `共 ${total.value} 条 · ${groups.value.length} 位干员`
})

/** 展开 / 收起；第一次展开时读一次 */
async function toggleList(): Promise<void> {
  if (expanded.value) {
    expanded.value = false
    return
  }
  expanded.value = true
  if (listLoaded.value) return
  await loadList()
}

/** 拉全部干员的记忆 */
async function loadList(): Promise<void> {
  if (listLoading.value) return
  listLoading.value = true
  listError.value = ''
  try {
    const snapshot = await fetchAllMemories()
    groups.value = snapshot.groups
    total.value = snapshot.total
    partialFailures.value = snapshot.failures
    mode.value = snapshot.mode
    // 列表接口能通 = 服务端可达：之前那些"读不到 / 没保存上"的提示都过时了
    modeLoadError.value = ''
    modeSaveError.value = ''
    listLoaded.value = true
  } catch (err) {
    listError.value = friendlyError(err)
  } finally {
    listLoading.value = false
  }
}

/** 头像（本地头像表里有就显示，没有就只显示名字） */
function avatarOf(name: string): string {
  return findCharacter(name)?.avatar ?? ''
}

// ---- 删除（二次确认） ---------------------------------------------------------
/** 正在等确认的那条 id（点「删除」→ 变成「确定删除 / 取消」） */
const confirmId = ref<number | null>(null)
/** 正在删的那条 id（删的时候按钮禁用，防连点） */
const deletingId = ref<number | null>(null)
/** 删除失败的一句话 */
const deleteError = ref('')

/** 点第一下：进入确认态（不直接发请求） */
function askDelete(item: MemoryItem): void {
  deleteError.value = ''
  confirmId.value = item.id
}

/** 取消确认 */
function cancelDelete(): void {
  confirmId.value = null
}

/** 点第二下：真删 */
async function doDelete(item: MemoryItem): Promise<void> {
  if (deletingId.value !== null) return
  deletingId.value = item.id
  deleteError.value = ''
  try {
    await deleteMemory(item.id)
    removeItem(item.id)
    confirmId.value = null
  } catch (err) {
    deleteError.value = `删除失败：${friendlyError(err)}`
  } finally {
    deletingId.value = null
  }
}

/** 从本地列表里摘掉一条（顺带清理空分组、维护总数） */
function removeItem(id: number): void {
  const next: MemoryGroup[] = []
  for (const group of groups.value) {
    const items = group.items.filter((item) => item.id !== id)
    if (items.length !== group.items.length) total.value = Math.max(0, total.value - 1)
    if (items.length > 0) next.push({ ...group, items })
  }
  groups.value = next
}

// ---- 新建对话时的本局记忆（本地偏好） -----------------------------------------
/** 当前选择（从 localStorage 读，缺省 inherit） */
const newChatMemory = ref<MemoryNewChat>(readNewChatMemory())

/** 三选一：跟随默认 / 开 / 关 */
const NEW_CHAT_SEG: SegOption[] = [
  { value: 'inherit', label: '跟随默认' },
  { value: 'on', label: '开' },
  { value: 'off', label: '关' },
]

/** 选择后的说明：重点写清"只对之后新建的对话生效" */
const newChatHint = computed(() => {
  if (newChatMemory.value === 'inherit') return '跟随上面的全局开关'
  return newChatMemory.value === 'on'
    ? '之后新建的对话一定读取记忆（即使全局关着）'
    : '之后新建的对话不读取记忆（写给她的账本仍在，只是这一局不用）'
})

/** 切换本局记忆：只落本地，建会话时才随请求发出去 */
function onNewChatChange(value: string): void {
  const next: MemoryNewChat = value === 'on' || value === 'off' ? value : 'inherit'
  newChatMemory.value = next
  writeNewChatMemory(next)
}

onMounted(loadMode)
</script>

<template>
  <div class="mem">
    <!-- ================= 1. 全局开关 ================= -->
    <SetSection title="干员记忆">
      <SetRow
        label="跨对话记得你"
        desc="她只记关于你的事：称呼、偏好、约定、你的身份背景；剧情不跨对话。"
        :hint="modeHint"
        :tone="modeTone"
      >
        <SegControl
          :model-value="mode"
          :options="MODE_SEG"
          :disabled="modeLoading || modeSaving"
          aria-label="干员记忆"
          @update:model-value="onToggleMode"
        />
      </SetRow>

      <!-- 读失败时给一个明确的"再试一次"（开关本身是禁用状态，重试才有意义） -->
      <SetRow
        v-if="modeLoadError && !modeLoading"
        label="重新读取设置"
        desc="刚才没能从服务器拿到当前的开关状态"
      >
        <button class="pn-pill pn-pill--ghost mem__btn" type="button" @click="loadMode">
          重试
        </button>
      </SetRow>

      <!-- ================= 2. 她记得什么 ================= -->
      <SetRow
        label="她记得什么"
        :desc="listSummary"
        hint="关掉开关也不会删账本：这里始终可以查看、可以单独删。删掉的不会被下一次提炼写回来。"
        tone="keep"
      >
        <button
          class="pn-pill pn-pill--ghost mem__btn"
          type="button"
          :aria-expanded="expanded"
          @click="toggleList"
        >
          {{ toggleLabel }}
        </button>
      </SetRow>

      <!-- 列表本体：只在展开后渲染，收起时不发请求、不占高度 -->
      <div v-if="expanded" class="mem__list">
        <p v-if="mode === 'off'" class="mem__notice">
          记忆已关闭 —— 她不会再记新的，但下面这些都是已经记下的，可以照常查看和删除。
        </p>

        <!-- 加载中 -->
        <p v-if="listLoading" class="mem__state">正在读取她的记忆…</p>

        <!-- 失败：一句人话 + 重试 -->
        <div v-else-if="listError" class="mem__state mem__state--error">
          <span>读取失败：{{ listError }}</span>
          <button class="pn-pill pn-pill--ghost mem__btn" type="button" @click="loadList">
            重试
          </button>
        </div>

        <!-- 空态 -->
        <p v-else-if="total === 0" class="mem__state">
          她还没记住关于你的事。多聊一阵，她才会把称呼、偏好、约定这些记下来。
        </p>

        <!-- 有内容：按干员分组 -->
        <template v-else>
          <p v-if="partialFailures > 0" class="mem__state mem__state--warn">
            有 {{ partialFailures }} 位干员的记忆没读到（网络抖动），可以点「收起 → 展开」再试一次。
          </p>

          <section v-for="group in groups" :key="group.id" class="mem__group">
            <header class="mem__group-head">
              <img v-if="avatarOf(group.name)" class="mem__avatar" :src="avatarOf(group.name)" :alt="group.name" />
              <span v-else class="mem__avatar mem__avatar--blank" aria-hidden="true">{{ group.name.slice(0, 1) }}</span>
              <span class="mem__group-name">{{ group.name }}</span>
              <span class="mem__group-count">{{ group.items.length }} 条</span>
            </header>

            <div
              v-for="item in group.items"
              :key="item.id"
              class="mem__item"
              :class="{ 'is-confirming': confirmId === item.id }"
            >
              <div class="mem__item-row">
                <span class="mem__kind">{{ memoryKindLabel(item.kind) }}</span>
                <span class="mem__text">{{ item.text }}</span>
                <span v-if="item.pinned" class="mem__pin">置顶</span>
                <time class="mem__time" :datetime="item.updated_at || item.created_at">
                  {{ formatMemoryTime(item.updated_at || item.created_at) }}
                </time>
                <button
                  v-if="confirmId !== item.id"
                  class="mem__del"
                  type="button"
                  :aria-label="`删除这条记忆：${item.text}`"
                  @click="askDelete(item)"
                >删除</button>
              </div>

              <!-- 二次确认：删除是不可撤销的（后端软删 + 留指纹，删了不会复活） -->
              <div v-if="confirmId === item.id" class="mem__confirm">
                <span class="mem__confirm-text">确定删除这条？删掉之后她不会再想起来。</span>
                <button
                  class="mem__confirm-yes"
                  type="button"
                  :disabled="deletingId === item.id"
                  @click="doDelete(item)"
                >{{ deletingId === item.id ? '删除中…' : '确定删除' }}</button>
                <button class="mem__confirm-no" type="button" @click="cancelDelete">取消</button>
              </div>
            </div>
          </section>

          <p v-if="deleteError" class="mem__state mem__state--error">{{ deleteError }}</p>
        </template>
      </div>
    </SetSection>

    <!-- ================= 3. 新建对话时的本局记忆 ================= -->
    <SetSection title="新建对话">
      <SetRow
        label="本局记忆"
        desc="只对之后新建的对话生效"
        :hint="newChatHint"
        tone="keep"
      >
        <SegControl
          :model-value="newChatMemory"
          :options="NEW_CHAT_SEG"
          aria-label="新建对话时的记忆"
          @update:model-value="onNewChatChange"
        />
      </SetRow>

      <!-- 这条限制必须写在界面上：用户以为能改当前这局，点了没反应就是 bug 报告 -->
      <SetRow
        label="已经开着的对话"
        desc="不会因为这里的设置改变"
        hint="后端没有「修改已有对话」的接口，记忆模式只在建对话那一刻写进去。要换一局，请新建一个对话。"
        tone="keep"
        off
      >
        <span class="mem__ro">—</span>
      </SetRow>
    </SetSection>
  </div>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

// 本面板完全沿用设置页的设计系统(.pn-sec / .pn-row / .pn-pill)，
// 这里只补「记忆列表」自己的三层结构：干员分组 → 条目行 → 删除确认条。
.mem {
  // 按钮在设置行的控件列里不需要额外撑宽，沿用 .pn-pill 即可
  &__btn {
    min-width: 96px;
  }

  // 只读占位（"已经开着的对话"那一行）
  &__ro {
    color: $color-text-faint;
    font-size: 13px;
  }

  // ---- 列表容器 ------------------------------------------------------------
  // 与 .pn-sec 同一套左右留白，展开后左边缘与上方设置行对齐
  &__list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 2px;
    padding: 2px 24px 4px;
  }

  &__notice {
    margin: 0;
    padding: 8px 12px;
    border-left: 3px solid $color-accent;
    border-radius: 3px;
    background: $color-accent-soft;
    color: $color-text-body;
    font-size: 12.5px;
    line-height: 1.6;
  }

  // ---- 加载 / 空 / 失败 三种状态 ------------------------------------------
  &__state {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 0;
    padding: 18px 16px;
    border: 1px dashed $color-line-soft;
    border-radius: 3px;
    color: $color-text-muted;
    font-size: 13px;
    line-height: 1.6;

    &--error {
      border-color: rgba($color-accent, 0.4);
      color: $color-accent;
    }

    &--warn {
      border-style: solid;
      color: $color-text-muted;
    }
  }

  // ---- 干员分组 ------------------------------------------------------------
  &__group {
    border: 1px solid $color-line-soft;
    border-radius: 3px;
    background: $color-surface-soft;
    overflow: hidden;
  }

  &__group-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    border-bottom: 1px solid $color-line-soft;
    background: $color-surface-soft;
  }

  &__avatar {
    flex: 0 0 auto;
    width: 26px;
    height: 26px;
    border-radius: 3px;
    object-fit: cover;
    background: $color-surface-soft;

    &--blank {
      display: flex;
      align-items: center;
      justify-content: center;
      color: $color-text-muted;
      font-size: 13px;
    }
  }

  &__group-name {
    color: $color-text-primary;
    font-size: 14.5px;
    line-height: 1.4;
  }

  &__group-count {
    margin-left: auto;
    color: $color-text-faint;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  // ---- 单条记忆 ------------------------------------------------------------
  &__item {
    & + & {
      border-top: 1px solid $color-line-soft;
    }

    &.is-confirming {
      background: $color-accent-soft;
    }
  }

  &__item-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 9px 14px;
  }

  // kind 标签：小胶囊，固定宽度让内容列对齐
  &__kind {
    flex: 0 0 auto;
    min-width: 42px;
    padding: 2px 8px;
    border-radius: 999px;
    background: $color-hover-overlay;
    color: $color-text-muted;
    font-size: 11.5px;
    line-height: 1.5;
    text-align: center;
  }

  &__text {
    flex: 1;
    min-width: 0;
    color: $color-text-primary;
    font-size: 13.5px;
    line-height: 1.6;
    word-break: break-word;
  }

  &__pin {
    flex: 0 0 auto;
    padding: 1px 6px;
    border: 1px solid $color-accent;
    border-radius: 2px;
    color: $color-accent;
    font-size: 10.5px;
    line-height: 1.5;
  }

  &__time {
    flex: 0 0 auto;
    color: $color-text-faint;
    font-size: 11.5px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  // 删除入口：平时低调，hover 才亮起来（避免整页全是红字/黄字按钮）
  &__del {
    flex: 0 0 auto;
    padding: 3px 10px;
    border: 1px solid $color-line-soft;
    border-radius: 999px;
    background: none;
    color: $color-text-muted;
    font-family: inherit;
    font-size: 12px;
    line-height: 1.5;
    cursor: pointer;
    transition: color 0.15s ease, border-color 0.15s ease;

    &:hover {
      border-color: $color-accent;
      color: $color-accent;
    }
  }

  // ---- 删除确认条 ----------------------------------------------------------
  &__confirm {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 8px 14px 10px;
    border-top: 1px dashed rgba($color-accent, 0.28);
  }

  &__confirm-text {
    flex: 1;
    min-width: 0;
    color: $color-accent;
    font-size: 12.5px;
    line-height: 1.5;
  }

  &__confirm-yes,
  &__confirm-no {
    flex: 0 0 auto;
    padding: 5px 14px;
    border-radius: 999px;
    font-family: inherit;
    font-size: 12.5px;
    line-height: 1.5;
    cursor: pointer;
    transition: background-color 0.15s ease, color 0.15s ease, opacity 0.15s ease;
  }

  &__confirm-yes {
    border: 1px solid $color-accent;
    background: $color-accent;
    color: $color-accent-ink;

    &:hover:not(:disabled) {
      opacity: 0.86;
    }

    &:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
  }

  &__confirm-no {
    border: 1px solid $color-line-soft;
    background: none;
    color: $color-text-body;

    &:hover {
      border-color: $color-text-muted;
      color: $color-text-primary;
    }
  }
}

// 窄屏：时间与删除按钮换到第二行，内容列不被挤成一列一个字
@media (max-width: 768px) {
  .mem {
    &__list {
      padding-left: 0;
      padding-right: 0;
    }

    &__item-row {
      flex-wrap: wrap;
      gap: 6px 10px;
    }

    &__text {
      flex: 1 1 100%;
      order: 2;
    }

    &__kind {
      order: 1;
    }

    &__time {
      order: 3;
      margin-left: auto;
    }

    &__del {
      order: 4;
    }
  }
}
</style>
