<script setup lang="ts">
// =============================================================================
// 免责声明弹窗(DisclaimerDialog)
// -----------------------------------------------------------------------------
// 三处入口共用这一份条款,文案只维护一次,避免两处条款走散:
//   · 开屏公告 NoticeDialog 里的「免责声明」
//   · 设置 → 关于 AboutPanel 底部的「《免责声明》完整条款」
//   · (将来)任何需要展示条款的地方
//
// 条款正文下面跟「加入 QQ 群」两张二维码(公告群 / 交流群):
//   条款和"有事找谁"是一件事的两半 —— 只给责任、不给去处,用户看完只剩焦虑。
//   二维码数据仍只取自 constants/contact(与关于面板同一份),不另写一遍。
// =============================================================================
import { onBeforeUnmount, ref } from 'vue'

import { QQ_GROUPS, type QqGroup } from '../../constants/contact'
import { copyText } from '../../utils/clipboard'
import { openQqGroup } from '../../utils/qqGroup'
import VersionDialogShell from './VersionDialogShell.vue'

withDefaults(
  defineProps<{
    /** 是否显示 */
    open: boolean
    /**
     * 层级
     *
     * 从「设置 → 关于」打开时要压过设置面板(300/301)，所以默认给 560；
     * 开屏公告里打开时本来就没有别的面板，560 也不会盖错东西。
     */
    layer?: number
  }>(),
  { layer: 560 },
)

const emit = defineEmits<{
  (e: 'close'): void
}>()

/** 刚复制过的群号(按钮文字反馈,1.6s 后复位) */
const copiedCode = ref('')
let copiedTimer: number | null = null

function onCopy(code: string): void {
  void copyText(code)
  copiedCode.value = code
  if (copiedTimer !== null) window.clearTimeout(copiedTimer)
  copiedTimer = window.setTimeout(() => {
    copiedCode.value = ''
    copiedTimer = null
  }, 1600)
}

/** 点二维码 / 「加群」→ 手机端唤起 QQ 群名片,其余环境开分享页 */
function onJoin(group: QqGroup): void {
  void openQqGroup(group)
}

onBeforeUnmount(() => {
  if (copiedTimer !== null) window.clearTimeout(copiedTimer)
})
</script>

<template>
  <VersionDialogShell
    :open="open"
    title="免责声明"
    kind="alert"
    :layer="layer"
    @close="emit('close')"
  >
    <!-- 加群二维码放在条款**前面**:条款很长,码放在最下面等于藏在折叠线以下
         (用户点「免责声明」是来查条款 + 找组织的,第一眼两样都要在)。
         数据与「关于」面板取自同一份 QQ_GROUPS。 -->
    <section class="ds__groups">
      <h3 class="ds__groups-h">加入 QQ 群</h3>
      <div class="ds__qrs">
        <div v-for="g in QQ_GROUPS" :key="g.code" class="ds__qr">
          <button
            class="ds__qr-imgbtn"
            type="button"
            :aria-label="`加入 ${g.label}`"
            @click="onJoin(g)"
          >
            <img class="ds__qr-img" :src="g.qr" :alt="`${g.label} 二维码`" loading="lazy" decoding="async" />
          </button>
          <p class="ds__qr-name">{{ g.label }}</p>
          <div class="ds__qr-foot">
            <button class="ds__qr-btn" type="button" @click="onCopy(g.code)">
              {{ copiedCode === g.code ? '已复制' : `群号 ${g.code}` }}
            </button>
            <a class="ds__qr-link" :href="g.url" rel="noopener" @click.prevent="onJoin(g)">
              加群 <span aria-hidden="true">↗</span>
            </a>
          </div>
        </div>
      </div>
    </section>

    <div class="ds__legal">
      <p><strong>一、用户责任与合规使用</strong></p>
      <p>您明确知晓并同意，您是使用本工具生成内容的唯一责任人。您承诺：</p>
      <p>1. 严格遵守您所使用 AI 模型服务商的所有使用政策与安全准则。</p>
      <p>2. 遵守您所在地及服务商所在地的现行法律法规，绝不利用本工具生成任何涉及政治敏感、淫秽色情、暴力恐怖、仇恨歧视、侵犯他人合法权益以及其他一切违法和不良信息。</p>
      <p>3. 理解并接受本工具仅用于合法的《明日方舟：终末地》同人角色扮演娱乐，任何超出此用途的使用风险自担。</p>

      <p><strong>二、知识产权与同人声明</strong></p>
      <p>《明日方舟：终末地》是上海鹰角网络科技有限公司的游戏产品。本工具为第三方同人作品，无任何盈利性质，与上海鹰角网络科技有限公司及《明日方舟：终末地》官方开发商、运营商无任何关联。</p>
      <p>本工具中使用的所有与《明日方舟：终末地》相关的角色形象、世界观设定、剧情元素、图片资源等知识产权，均归上海鹰角网络科技有限公司所有。本工具仅供爱好者学习与交流，严禁用于任何商业用途。</p>

      <p><strong>三、免责条款</strong></p>
      <p>在法律允许的最大范围内，本工具开发者不对以下情况承担任何明示或默示的担保或责任：</p>
      <p>1. 用户因违反本声明或第三方服务商条款而产生的任何纠纷、处罚或损失；</p>
      <p>2. 用户因篡改代码等自主行为所引发的一切后果；</p>
      <p>3. 对第三方 AI 模型服务商提供的服务质量、内容准确性及合规性。</p>
      <p>请您在使用前务必仔细阅读并同意以上全部条款。继续使用即代表您已充分理解并自愿承担所有相关风险。</p>
    </div>

    <template #actions>
      <button class="pn-btn" type="button" @click="emit('close')">
        <span class="pn-btn__tex" aria-hidden="true"></span>
        <span class="pn-btn__deco" aria-hidden="true"></span>
        <span class="pn-btn__label">我知道了</span>
      </button>
    </template>
  </VersionDialogShell>
</template>

<style scoped lang="scss">
@use '../../styles/variables' as *;

// -----------------------------------------------------------------------------
// 条款正文
// -----------------------------------------------------------------------------
// 这一层是**白底弹窗**(VersionDialogShell 的 .fx__body),所以字色用
// $color-paper-ink 而不是主题里的白字 —— 否则白底白字。
.ds__legal {
  color: $color-paper-ink;
  font-family: $font-harmony;
  font-size: 13.5px;
  line-height: 1.8;

  p {
    margin: 0 0 8px;

    &:last-child {
      margin-bottom: 0;
    }
  }

  strong {
    display: block;
    margin-top: 10px;
    color: $color-paper-ink-strong;
    font-size: 14.5px;
  }
}

// -----------------------------------------------------------------------------
// 加群二维码
// -----------------------------------------------------------------------------
// 白底上的卡片:细墨线 + 极浅底,二维码本身是深色卡片图,不用再套深底
// (深底会把码的静区吃掉,反而扫不出来)。
.ds__groups {
  margin-bottom: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid $color-paper-line;
}

.ds__groups-h {
  margin: 0 0 10px;
  color: $color-paper-ink-strong;
  font-family: $font-harmony;
  font-size: 14.5px;
  font-weight: 700;
}

.ds__qrs {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  // 弹窗正文比两张码宽得多:居中放,别让它们孤零零贴在左边
  max-width: 430px;
  margin: 0 auto;
}

.ds__qr {
  overflow: hidden;
  border: 1px solid $color-paper-line;
  border-radius: 8px;
  background: rgba(17, 24, 39, 0.03);
}

.ds__qr-imgbtn {
  display: block;
  width: 100%;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;

  &:focus-visible {
    outline: 1px dashed rgba(17, 24, 39, 0.5);
    outline-offset: -3px;
  }
}

.ds__qr-img {
  display: block;
  width: 100%;
  height: auto;
}

.ds__qr-name {
  margin: 0;
  padding: 6px 8px 0;
  color: $color-paper-ink;
  font-family: $font-harmony;
  font-size: 11.5px;
  line-height: 1.5;
}

.ds__qr-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 8px 7px;
}

.ds__qr-btn,
.ds__qr-link {
  padding: 0;
  border: 0;
  background: none;
  color: rgba(17, 24, 39, 0.6);
  font-family: inherit;
  font-size: 11.5px;
  line-height: 1.5;
  white-space: nowrap;
  cursor: pointer;
  transition: color 0.15s ease;
}

.ds__qr-btn {
  font-variant-numeric: tabular-nums;

  &:hover {
    color: $color-paper-ink-strong;
  }
}

.ds__qr-link {
  color: #8a6508;
  text-decoration: none;

  &:hover {
    color: $color-paper-ink-strong;
  }
}

// 手机上两张码并排会窄到扫不动,改成一列
@media (max-width: 480px) {
  .ds__qrs {
    grid-template-columns: 1fr;
    max-width: 240px;
  }
}
</style>
