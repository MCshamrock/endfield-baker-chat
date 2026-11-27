<script setup lang="ts">
// =============================================================================
// 首次打开公告弹窗(NoticeDialog)
// -----------------------------------------------------------------------------
// 每次打开应用弹出,直到用户点击"不再提醒"(持久化标记);点"确定"仅关闭本次。
// 正文来源: 优先使用 App 从网络公告源(notice.peilika.beer 的 popup.json)读取的
//           内容(prop content/title),读取失败/离线时回退内置常量。
// 外观: 进入弹窗统一外壳(VersionDialogShell)—— 品牌条 + 深灰标题栏 + 白底正文。
//
// 3.2.2 的两处变化:
//   1. 底部「全部公告」按钮删掉 —— 公告页已并进「设置 → 关于」,不再有独立页面;
//   2. 免责声明正文改为共用 DisclaimerDialog —— 以前这个文件里**又写了一遍**
//      完整条款,和「设置 → 免责声明」页各存一份,改一处漏一处。现在条款只在
//      DisclaimerDialog 里维护,这里只留一个入口链接。
// (公告 JSON 的读取链路完全没动:App → usePopups → 本组件,见 docs/05 §8)
// =============================================================================
import { ref, computed } from 'vue'
import {
  NOTICE_TITLE,
  NOTICE_CONTENT,
  NOTICE_CONFIRM_TEXT,
  NOTICE_DISMISS_TEXT,
} from '../../constants/notice'
import VersionDialogShell from './VersionDialogShell.vue'
import DisclaimerDialog from './DisclaimerDialog.vue'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 弹窗正文(App 从网络公告源读取;缺省回退内置常量) */
  content?: string
  /** 弹窗标题(网络公告源 JSON 的 title;空则回退内置标题) */
  title?: string
  /** 版本号(左上角徽标,如 v3.1) */
  version?: string
  /** 日期文字 */
  date?: string
}>()

const emit = defineEmits<{
  (e: 'confirm'): void   // 确认(本次关闭)
  (e: 'dismiss'): void   // 不再提醒(永久)
}>()

/** 展示标题:网络 JSON 的 title 优先,为空回退内置常量 */
const displayTitle = computed(() => props.title?.trim() || NOTICE_TITLE)

/** 展示正文:外部公告源优先(响应式跟随),为空/失败时回退内置常量 */
const displayContent = computed(() => props.content?.trim() || NOTICE_CONTENT)

/** 免责声明弹窗是否打开(与「设置 → 关于」共用同一个组件) */
const showDisclaimer = ref(false)

/** 确认:仅关闭本次弹窗(由父级复位 open) */
function onConfirm() {
  emit('confirm')
}

/** 不再提醒:永久关闭(由父级写入持久化标记) */
function onDismiss() {
  emit('dismiss')
}

/** 打开 / 关闭免责声明 */
function openDisclaimer() {
  showDisclaimer.value = true
}

function closeDisclaimer() {
  showDisclaimer.value = false
}
</script>

<template>
  <VersionDialogShell
    :open="open"
    :title="displayTitle"
    kind="notice"
    :version="version"
    :date="date"
    @close="onConfirm"
  >
    <p class="nd__text">{{ displayContent }}</p>

    <p class="nd__hint">
      点击「{{ NOTICE_CONFIRM_TEXT }}」或「{{ NOTICE_DISMISS_TEXT }}」即代表你已阅读和理解
      《<button class="nd__link" type="button" @click="openDisclaimer">免责声明</button>》
    </p>

    <template #actions>
      <button class="pn-btn pn-btn--light" type="button" @click="onDismiss">
        <span class="pn-btn__label">{{ NOTICE_DISMISS_TEXT }}</span>
      </button>
      <button class="pn-btn" type="button" @click="onConfirm">
        <span class="pn-btn__tex" aria-hidden="true"></span>
        <span class="pn-btn__deco" aria-hidden="true"></span>
        <span class="pn-btn__label">{{ NOTICE_CONFIRM_TEXT }}</span>
      </button>
    </template>
  </VersionDialogShell>

  <!-- 免责声明:与「设置 → 关于」里点开的**同一份组件**,层级 560 压过其它面板 -->
  <DisclaimerDialog :open="showDisclaimer" @close="closeDisclaimer" />
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

// 这一层是白底弹窗(VersionDialogShell 的 .fx__body),所以字色用 paper 那组
// 墨色 token,不能用主题里的白字。
.nd {
  &__text {
    margin: 0;
    color: $color-paper-ink;
    font-family: $font-harmony;
    font-size: 14px;
    line-height: 1.75;
    white-space: pre-line;
  }

  &__hint {
    margin: 18px 0 0;
    color: rgba(17, 24, 39, 0.55);
    font-family: $font-harmony;
    font-size: 12px;
    line-height: 1.6;
  }

  &__link {
    padding: 0;
    border: 0;
    background: none;
    color: #b8860b;
    font-family: inherit;
    font-size: inherit;
    text-decoration: underline;
    cursor: pointer;

    &:hover {
      color: #8a6508;
    }
  }
}
</style>
