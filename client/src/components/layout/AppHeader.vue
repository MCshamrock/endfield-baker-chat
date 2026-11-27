<script setup lang="ts">
// =============================================================================
// 页面顶栏(AppHeader)
// -----------------------------------------------------------------------------
// 一条紧贴页面顶端的横栏,同时承载两样东西:
//   左:品牌标题「//BAKER/会话消息」(+ 上方的小装饰条)
//   中:单聊 / 群聊 页签 + 尾格「设置」键 —— 三格同级,整条控制带居中
//
// 为什么把设置键放进这条控制带(3.2.2 的改动):
//   设置原来挂在左侧列表上方的操作带里,和「新建 / 删除 / 公告 / 用量」挤在一起,
//   一个"全局入口"看起来像"针对当前列表的动作"。移到顶栏后,它和「单聊 / 群聊」
//   并排成为三格:前两格是"我在看哪一类会话",第三格是"整站的设置",
//   层级一眼清楚,列表那条带子也还给了真正的动作按钮。
//   设置键是尾格,不是模式:没有选中黄块、不参与 select()。
//
// 页签**相对页面居中**(flex 居中于整条横栏),不是相对聊天区居中。
// =============================================================================
import { MATERIALS } from '../../constants/materials'
import { SETTINGS_ENTRY_ICON } from '../../constants/settingsNav'
import ChatModeTabs from '../chat/ChatModeTabs.vue'
import type { ChatMode } from '../chat/ChatModeTabs.vue'

const props = withDefaults(
  defineProps<{
    /** 当前会话模式 */
    mode: ChatMode
    /** 端:桌面端走设计画布绝对定位,移动端是贴视口顶部的 fixed 条 */
    variant?: 'desktop' | 'mobile'
  }>(),
  { variant: 'desktop' },
)

const emit = defineEmits<{
  (e: 'update:mode', mode: ChatMode): void
  /** 点控制带尾格的设置键:请求打开设置面板 */
  (e: 'open-settings'): void
}>()
</script>

<template>
  <header class="app-bar" :class="`app-bar--${props.variant}`">
    <div class="app-bar__brand">
      <img class="app-bar__deco" :src="MATERIALS.headerDeco" alt="" />
      <p class="app-bar__title">//BAKER/会话消息</p>
    </div>

    <ChatModeTabs :mode="mode" @update:mode="emit('update:mode', $event)">
      <template #trailing>
        <!-- 图标与设置面板第一个页签共用同一份数据(SETTINGS_ENTRY_ICON):
             点进来看到的第一个图标就是刚才点的那个 -->
        <button
          class="pn-tab pn-tab--settings"
          type="button"
          aria-label="设置"
          title="设置"
          @click="emit('open-settings')"
        >
          <span class="pn-tab__icon">
            <svg viewBox="0 0 48 48" aria-hidden="true">
              <path
                v-for="(p, i) in SETTINGS_ENTRY_ICON"
                :key="i"
                :d="p.d"
                :fill="p.filled ? 'currentColor' : 'none'"
                :stroke="p.filled ? 'none' : 'currentColor'"
                stroke-width="3.4"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </span>
        </button>
      </template>
    </ChatModeTabs>
  </header>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

.app-bar {
  position: absolute;
  left: 0;
  top: 0;
  display: flex;
  align-items: flex-start;
  // 控制带(页签 + 设置键)相对整条横栏居中 = 相对页面居中
  justify-content: center;
  pointer-events: none;

  /**
   * 顶栏这条控制带不画格子分隔线
   *
   * .pn-tab 的分隔线是给弹窗里的页签用的(那一排是"分类",需要竖线分段)。
   * 顶栏只有「单聊 / 群聊 / 设置」三格、靠图标与留白就能读清,
   * 加线反而把一条完整背板切成三段小方块。
   */
  :deep(.pn-tab)::after {
    display: none;
  }

  // 顶栏本身完全透明:斜纹只来自页签自己的 .pn-tabs__backdrop(与设置弹窗一致),
  // 这里再画一层就会变成两层斜纹。
  &__brand {
    position: absolute;
    left: 43px;
    top: 50%;
    transform: translateY(-50%);
    display: flex;
    flex-direction: column;
    gap: 2px;
    pointer-events: none;
  }

  &__deco {
    display: block;
    width: 63px;
    height: 12px;
  }

  &__title {
    margin: 0;
    line-height: 1;
    white-space: nowrap;
    color: $color-text-primary;
    font-size: $font-size-title;
    font-weight: 500;
    letter-spacing: -0.5px;
    user-select: text;
  }

  // ---- 桌面端:1920 设计画布坐标 ------------------------------------------
  // 顶栏整体加高,**宽高同比放大**(页签 66×56 → 86×72),但里面的图标/文字
  // 尺寸保持原样。页签背板仍紧贴页面顶端(与设置菜单一致),图标靠 flex 居中
  // 自动下沉,不会贴住上边缘。
  &--desktop {
    width: 1920px;
    height: 80px;
    z-index: 4;

    :deep(.pn-tabs) {
      height: 72px;
    }

    // 三格(单聊 / 群聊 / 设置)必须同宽:图标在各格居中,格宽不等时
    // 图标中心间距 = 两格半宽之和,最后一格就会明显比前两格挤。
    :deep(.pn-tab) {
      width: 86px;
      height: 72px;
    }
  }

  // ---- 移动端:贴视口顶部的固定条 ----------------------------------------
  &--mobile {
    position: fixed;
    // 必须同时给 left/right:只写 right 时宽度会缩到内容宽、整条贴在右边缘,
    // 左边的标题就会和页签叠在一起(3.1 的写法)。
    left: 0;
    right: 0;
    width: auto;
    height: calc(50px + var(--safe-top, 0px));
    padding-top: var(--safe-top, 0px);
    box-sizing: border-box;
    z-index: 120;
    /**
     * 控制带**整条横栏居中**(与桌面顶栏一致)。
     *
     * 以前为了躲开左边的标题写死成 flex-end,结果在 390~430 宽的手机上
     * 整条带子贴着右边缘、中间空出一大片,看着像"顶栏滑到右边去了"。
     * 改成居中后,标题必须自己让位(否则会钻到带子底下):见下面的 max-width。
     */
    justify-content: center;

    .app-bar__brand {
      left: 12px;
      gap: 1px;
      /**
       * 垂直居中要相对「安全区以下的那 50px」算，而不是相对整条（50 + safe-top）：
       * 顶栏是 padding-top 让位，绝对定位的子元素按整个 padding box 居中，
       * 直接用 top:50% 会让标题往上钻进状态栏一截（挖孔屏上很明显）。
       */
      top: calc(var(--safe-top, 0px) + 25px);
      /**
       * 标题可用宽度 = 控制带左边界 − 左边距 − 呼吸缝
       *   带子宽度 = 3 格 × 48 + 左右 padding 8×2 = 160
       *   带子左边界 = 50vw − 80
       *   → 100 = 80 + 12(左边距) + 8(呼吸缝)
       * 超出的部分用省略号截断,绝不钻到页签下面。
       */
      max-width: calc(50vw - 100px);
    }

    .app-bar__deco {
      width: 40px;
      height: 8px;
    }

    .app-bar__title {
      max-width: 100%;
      // 12px:400 宽屏幕上「//BAKER/会话消息」正好塞进居中后剩下的空间
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    // 手机屏窄:页签不能再按桌面比例放大,否则会压到左边的标题。
    // 这里取 48×50(触控仍够大:48 ≥ 44 的最小推荐值),同样紧贴页面顶端,
    // 图标靠居中自动下沉。设置键同宽 —— 三格等宽,图标中心间距才均匀。
    :deep(.pn-tabs) {
      height: 50px;
      padding: 0 8px;
    }

    :deep(.pn-tab) {
      width: 48px;
      height: 50px;
    }

    :deep(.pn-tab__icon) {
      width: 20px;
      height: 20px;
    }
  }
}
</style>
