<script setup lang="ts">
// =============================================================================
// LegacyMigrateBanner —— 老版本地数据迁移提示条
// -----------------------------------------------------------------------------
// 为什么是"条"而不是弹窗：
//   迁移是一次性动作，但用户可能想先聊两句、先看看新界面。弹窗会挡住整个应用，
//   "稍后再说"之后还得记着回来。提示条固定在顶部，不挡操作，随时能点，也能
//   一键收掉（本次会话不再出现）。
//
// 它只做三件事：
//   1. 启动时检测（只读，不改不删老数据）
//   2. 有数据 → 提示 +「立即迁移 / 先备份一份 / 稍后再说」
//   3. 迁移 → 进度 → 摘要（几段会话/几条消息/几张图/跳过了什么）
//
// 数据归属：迁移到服务端后，已登录的挂账号、访客挂设备（之后登录会自动收编）。
// 所以文案不能说"出现在左侧列表"—— 列表马上要改成服务端驱动，这里只说"账号里"。
// =============================================================================

import { computed, onMounted, ref } from 'vue'
import { useAccountStore } from '../../stores/account'
import { useListStore } from '../../stores/list'
import {
  cleanupLegacyServiceWorkersOnce,
  describeMigration,
  describeMigrationError,
  detectLegacyLocal,
  downloadLegacyBackup,
  migrateLegacyLocal,
  readLegacyMigrateMarker,
} from '../../utils/legacyLocalData'
import type {
  LegacyDetectSummary,
  MigrationOutcome,
  MigrationProgress,
} from '../../utils/legacyLocalCore'
import { devWarn } from '../../utils/logger'

/** 「稍后再说」只记在会话里：刷新之后还会再问一次（不是永久拒绝） */

const listStore = useListStore()
const SNOOZE_KEY = 'bakerai.legacy-migrate.snoozed'

const account = useAccountStore()

type Phase = 'detecting' | 'idle' | 'ready' | 'migrating' | 'success' | 'error'

const phase = ref<Phase>('detecting')
const summary = ref<(LegacyDetectSummary & { local: unknown }) | null>(null)
const progress = ref<MigrationProgress | null>(null)
const outcome = ref<Extract<MigrationOutcome, { skipped: false }> | null>(null)
const errorText = ref('')
/** 迁移前已经迁移过（标记存在）：直接不出现，连检测结果都不用留 */
const alreadyMigrated = ref(false)
/** 用户关掉了（本次会话） */
const dismissed = ref(false)
const backupBusy = ref(false)
const backupNote = ref('')

const visible = computed(() => {
  if (dismissed.value) return false
  if (alreadyMigrated.value) return false
  return phase.value === 'ready' || phase.value === 'migrating' ||
    phase.value === 'success' || phase.value === 'error'
})

const accountLabel = computed(() => {
  if (account.state === 'user' && account.user) {
    return account.user.display_name || account.user.username
  }
  return ''
})

/** 归属说明：登录了就挂账号；访客先挂设备，之后登录会自动收编（web/back docs/03 §2） */
const ownerText = computed(() => {
  const name = accountLabel.value
  if (name) return `迁移完成后，数据会出现在账号「${name}」里（服务端），换设备登录也能看到。`
  return '迁移完成后，数据会先挂在这台设备名下（服务端）；之后在这台设备登录账号，会自动并入账号。'
})

const titleText = computed(() => {
  const s = summary.value
  if (!s) return ''
  return (
    `发现旧版本地数据：${s.cards} 张卡 / ${s.conversations} 段对话 / ${s.messages} 条消息` +
    ' —— 迁移到账号（可稍后）'
  )
})

const detailText = computed(() => {
  const s = summary.value
  if (!s) return ''
  const parts: string[] = []
  if (s.contextFallback > 0) {
    parts.push(`其中 ${s.contextFallback} 条来自 AI 记忆兜底（这些对话的屏上消息被清空过）`)
  }
  if (s.skipped > 0) parts.push(`会跳过 ${s.skipped} 条失败气泡/空消息（本来就不算聊天记录）`)
  if (s.promptOverrides > 0) parts.push(`另有 ${s.promptOverrides} 条自定义角色提示词一起迁移`)
  return parts.join('；')
})

const successText = computed(() => {
  const o = outcome.value
  return o ? describeMigration(o) : ''
})

const progressText = computed(() => {
  const p = progress.value
  if (!p) return '正在准备…'
  const piece = p.piece && p.pieces && p.pieces > 1 ? `（第 ${p.piece}/${p.pieces} 批）` : ''
  if (p.phase === 'reading') return '正在读取旧数据…'
  if (p.phase === 'uploading') {
    const total = p.total && p.total > 0 ? formatBytes(p.total) : ''
    const percent = typeof p.percent === 'number' && p.percent >= 0 ? ` ${p.percent}%` : ''
    return `正在上传${total ? ` ${total}` : ''}${percent}${piece}`
  }
  return `服务器正在写入${piece}`
})

const progressPercent = computed(() => {
  const p = progress.value
  if (!p) return 0
  if (p.phase === 'processing') return 100
  return typeof p.percent === 'number' && p.percent >= 0 ? p.percent : 0
})

function formatBytes(bytes: number): string {
  if (!bytes || bytes < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value < 10 && index > 0 ? value.toFixed(1) : Math.round(value)} ${units[index]}`
}

function snoozed(): boolean {
  try {
    return sessionStorage.getItem(SNOOZE_KEY) === '1'
  } catch {
    return false
  }
}

onMounted(() => {
  // 覆盖部署善后：老站的 Service Worker / 缓存可能还在喂旧资源（见 utils 里的注释）
  void cleanupLegacyServiceWorkersOnce()

  // 已经迁移过 → 什么都不做（不读库、不弹条）
  if (readLegacyMigrateMarker()) {
    alreadyMigrated.value = true
    phase.value = 'idle'
    return
  }
  if (snoozed()) {
    phase.value = 'idle'
    return
  }

  void (async () => {
    try {
      const detected = await detectLegacyLocal()
      summary.value = detected
      if (!detected.hasData) {
        phase.value = 'idle'
        return
      }
      phase.value = 'ready'
    } catch (err) {
      // 检测失败不该打扰用户：新站照常能用，老数据还在浏览器里
      devWarn('[legacy] 检测旧数据失败', err)
      phase.value = 'idle'
    }
  })()
})

async function startMigrate(): Promise<void> {
  if (phase.value === 'migrating') return
  phase.value = 'migrating'
  errorText.value = ''
  progress.value = { phase: 'reading' }
  try {
    const result = await migrateLegacyLocal({
      onProgress: (p) => {
        progress.value = p
      },
    })
    if (result.skipped) {
      // 标记已经存在（另一个标签页刚导过 / 上次成功但界面没关）
      alreadyMigrated.value = true
      phase.value = 'idle'
      return
    }
    outcome.value = result
    phase.value = 'success'
    // 迁移完必须让列表刷新（并在允许时切到服务端）。
    // 2026-11-18 实测：只写标记不刷新，用户看到的本机列表还是空的，
    // 会以为"点了迁移数据就没了"（其实数据已经在服务端）。
    await listStore.afterClaim()
  } catch (err) {
    errorText.value = describeMigrationError(err)
    phase.value = 'error'
  } finally {
    progress.value = null
  }
}

async function backup(): Promise<void> {
  if (backupBusy.value) return
  backupBusy.value = true
  backupNote.value = ''
  try {
    const result = await downloadLegacyBackup()
    backupNote.value =
      `已备份：${result.filename}（${formatBytes(result.bytes)}，` +
      `${result.counts.conversations} 段对话 / ${result.counts.messages} 条消息）`
  } catch (err) {
    backupNote.value = err instanceof Error ? err.message : '备份失败，请重试'
  } finally {
    backupBusy.value = false
  }
}

function snooze(): void {
  try {
    sessionStorage.setItem(SNOOZE_KEY, '1')
  } catch {
    // 存不进就只在内存里生效（本次关闭）
  }
  dismissed.value = true
}

function dismiss(): void {
  dismissed.value = true
}
</script>

<template>
  <!-- 非阻断提示条：固定在顶部，不挡操作；收掉后本次会话不再出现 -->
  <div v-if="visible" class="legacy-banner" role="status" aria-live="polite">
    <div class="legacy-banner__inner">
      <span class="legacy-banner__tag" aria-hidden="true">旧数据</span>

      <div class="legacy-banner__body">
        <!-- 检测到 / 迁移中 -->
        <template v-if="phase === 'ready' || phase === 'migrating'">
          <p class="legacy-banner__title">{{ titleText }}</p>
          <p class="legacy-banner__line">{{ ownerText }}</p>
          <p v-if="detailText" class="legacy-banner__line legacy-banner__line--dim">{{ detailText }}</p>
          <p class="legacy-banner__line legacy-banner__line--warn">
            迁移只往新版后端写一份，不会删除或改动浏览器里的旧数据（随时可以回滚）。
          </p>

          <div v-if="phase === 'migrating'" class="legacy-banner__progress">
            <div class="legacy-banner__track">
              <div
                class="legacy-banner__bar"
                :class="{ 'is-indeterminate': progressPercent <= 0 }"
                :style="progressPercent > 0 ? { width: progressPercent + '%' } : undefined"
              />
            </div>
            <span class="legacy-banner__progress-text">{{ progressText }}</span>
          </div>
        </template>

        <!-- 成功 -->
        <template v-else-if="phase === 'success'">
          <p class="legacy-banner__title">{{ successText }}</p>
          <ul v-if="outcome && outcome.notes.length > 0" class="legacy-banner__notes">
            <li v-for="note in outcome.notes" :key="note">{{ note }}</li>
          </ul>
          <p class="legacy-banner__line legacy-banner__line--warn">
            老数据仍保留在浏览器里（不会被自动清理）。确认账号里的记录没问题后，再自行决定要不要清本地。
          </p>
          <p v-if="backupNote" class="legacy-banner__line legacy-banner__line--dim">{{ backupNote }}</p>
        </template>

        <!-- 失败 -->
        <template v-else-if="phase === 'error'">
          <p class="legacy-banner__title">迁移没有完成</p>
          <p class="legacy-banner__line legacy-banner__line--error">{{ errorText }}</p>
          <p v-if="backupNote" class="legacy-banner__line legacy-banner__line--dim">{{ backupNote }}</p>
        </template>
      </div>

      <div class="legacy-banner__actions">
        <!-- 待迁移 -->
        <template v-if="phase === 'ready'">
          <button class="legacy-banner__btn legacy-banner__btn--primary" type="button" @click="startMigrate">
            立即迁移
          </button>
          <button class="legacy-banner__btn" type="button" :disabled="backupBusy" @click="backup">
            {{ backupBusy ? '备份中…' : '先备份一份' }}
          </button>
          <button class="legacy-banner__btn legacy-banner__btn--ghost" type="button" @click="snooze">
            稍后再说
          </button>
        </template>

        <!-- 迁移中 -->
        <template v-else-if="phase === 'migrating'">
          <span class="legacy-banner__hint">迁移中，请不要关闭页面…</span>
        </template>

        <!-- 成功 -->
        <template v-else-if="phase === 'success'">
          <button class="legacy-banner__btn" type="button" :disabled="backupBusy" @click="backup">
            {{ backupBusy ? '备份中…' : '再备份一份' }}
          </button>
          <button class="legacy-banner__btn legacy-banner__btn--primary" type="button" @click="dismiss">
            知道了
          </button>
        </template>

        <!-- 失败 -->
        <template v-else-if="phase === 'error'">
          <button class="legacy-banner__btn legacy-banner__btn--primary" type="button" @click="startMigrate">
            重试
          </button>
          <button class="legacy-banner__btn" type="button" :disabled="backupBusy" @click="backup">
            {{ backupBusy ? '备份中…' : '先备份一份' }}
          </button>
          <button class="legacy-banner__btn legacy-banner__btn--ghost" type="button" @click="dismiss">
            关掉
          </button>
        </template>
      </div>
    </div>

    <!-- 备份提示：不占按钮行，跟着条走 -->
    <p v-if="phase === 'ready' && backupNote" class="legacy-banner__backup">{{ backupNote }}</p>
  </div>
</template>

<style scoped>
/* 固定顶部条：不铺遮罩 → 应用照常可点可用（非阻断） */
.legacy-banner {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 400;
  background: rgba(20, 20, 19, 0.96);
  border-bottom: 2px solid #ffef00;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.45);
  color: #f0eeee;
  font-family: 'HarmonyOS Sans SC Medium', 'MiSans', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  padding: 10px 16px calc(10px + env(safe-area-inset-bottom, 0px));
}

.legacy-banner__inner {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  max-width: 1280px;
  margin: 0 auto;
}

.legacy-banner__tag {
  flex: none;
  margin-top: 2px;
  padding: 2px 8px;
  background: #ffef00;
  color: #1c1c1c;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1px;
  border-radius: 2px;
}

.legacy-banner__body {
  flex: 1 1 auto;
  min-width: 0;
}

.legacy-banner__title {
  margin: 0 0 4px;
  font-size: 14px;
  line-height: 1.5;
  color: #fff;
}

.legacy-banner__line {
  margin: 2px 0;
  font-size: 12px;
  line-height: 1.6;
  color: #d9d7d2;
}

.legacy-banner__line--dim {
  color: #a8a6a0;
}

.legacy-banner__line--warn {
  color: #ffef00;
}

.legacy-banner__line--error {
  color: #ff8f8f;
  white-space: pre-line;
}

.legacy-banner__notes {
  margin: 4px 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.6;
  color: #d9d7d2;
}

.legacy-banner__actions {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
  max-width: 340px;
}

.legacy-banner__btn {
  padding: 6px 14px;
  font-size: 13px;
  line-height: 1.4;
  color: #1c1c1c;
  background: #f0eeee;
  border: 1px solid #f0eeee;
  border-radius: 2px;
  cursor: pointer;
  white-space: nowrap;
}

.legacy-banner__btn:hover:not(:disabled) {
  background: #fff;
}

.legacy-banner__btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.legacy-banner__btn--primary {
  background: #ffef00;
  border-color: #ffef00;
  font-weight: 700;
}

.legacy-banner__btn--ghost {
  background: transparent;
  border-color: #6d6b66;
  color: #d9d7d2;
}

.legacy-banner__btn--ghost:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
}

.legacy-banner__hint {
  font-size: 12px;
  color: #a8a6a0;
}

.legacy-banner__progress {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}

.legacy-banner__track {
  position: relative;
  flex: 1 1 200px;
  height: 4px;
  background: rgba(255, 255, 255, 0.16);
  border-radius: 2px;
  overflow: hidden;
}

.legacy-banner__bar {
  height: 100%;
  background: #ffef00;
  transition: width 0.2s ease;
}

/* 拿不到百分比（fetch 上传）时走来回扫的动画，而不是一条永远 0% 的死条 */
.legacy-banner__bar.is-indeterminate {
  width: 35%;
  animation: legacy-slide 1.2s ease-in-out infinite;
}

@keyframes legacy-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(320%); }
}

.legacy-banner__progress-text {
  flex: none;
  font-size: 12px;
  color: #d9d7d2;
}

.legacy-banner__backup {
  max-width: 1280px;
  margin: 6px auto 0;
  padding-left: 46px;
  font-size: 12px;
  color: #a8a6a0;
}

@media (max-width: 768px) {
  .legacy-banner {
    padding: 8px 10px calc(8px + env(safe-area-inset-bottom, 0px));
  }

  .legacy-banner__inner {
    flex-wrap: wrap;
    gap: 8px;
  }

  .legacy-banner__tag {
    margin-top: 0;
  }

  .legacy-banner__actions {
    flex: 1 1 100%;
    max-width: none;
    justify-content: flex-start;
  }

  .legacy-banner__btn {
    padding: 5px 10px;
    font-size: 12px;
  }

  .legacy-banner__backup {
    padding-left: 0;
  }
}
</style>