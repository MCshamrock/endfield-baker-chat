<script setup lang="ts">
/**
 * UsersView.vue —— 账号管理。
 *
 * 三个动作都在行内：改显示名、改角色、改状态。为什么不做"点进详情页再改"：
 * 全站就几十个账号，行内改一次不到两秒；跳页面反而更慢更容易改错人。
 *
 * 两条安全规则写在这一层：
 *  1. **不能把自己降权**（后端会 400，前端先拦住并说清楚为什么）；
 *  2. 也不能在这里停用自己 —— 后端没拦，但停用自己等于把自己锁在门外。
 */
import { computed, onMounted, ref } from 'vue'
import StateBlock from '../components/StateBlock.vue'
import { toast } from '../assets/endfield/fx.js'
import { api, ApiError } from '../lib/api.ts'
import { fmtDate, fmtDateTime, fmtInt } from '../lib/format.ts'
import type { AdminUser } from '../lib/types.ts'

const props = defineProps<{
  /** 当前登录的管理员用户名：用来判断"这一行是我自己" */
  me: string
}>()

const emit = defineEmits<{ 'self-renamed': [displayName: string] }>()

const rows = ref<AdminUser[]>([])
const total = ref(0)
const limit = ref(20)
const offset = ref(0)
const query = ref('')
/** 已经生效的搜索词（输入框里的字不一定是查的那个） */
const appliedQuery = ref('')
const loading = ref(true)
const error = ref('')
/** 正在提交的那一行（用来禁用控件，别让人连点两次） */
const busyId = ref('')
/** 每行显示名的草稿，按 public_id 存 */
const drafts = ref<Record<string, string>>({})

const page = computed(() => Math.floor(offset.value / limit.value) + 1)
const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)))
const isMyself = (row: AdminUser) => row.username === props.me

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await api.listUsers({ q: appliedQuery.value, limit: limit.value, offset: offset.value })
    rows.value = result.items
    total.value = result.total
    // 每次拿到新数据都重置草稿：草稿只属于"这一屏的这一份数据"
    drafts.value = Object.fromEntries(result.items.map((r) => [r.id, r.display_name]))
  } catch (err) {
    error.value = err instanceof ApiError ? err.message : '账号列表加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function search() {
  appliedQuery.value = query.value.trim()
  offset.value = 0
  void load()
}

function clearSearch() {
  query.value = ''
  search()
}

function goPage(delta: number) {
  const next = offset.value + delta * limit.value
  if (next < 0 || next >= total.value) return
  offset.value = next
  void load()
}

function changeLimit(event: Event) {
  limit.value = Number((event.target as HTMLSelectElement).value)
  offset.value = 0
  void load()
}

/** 改一列并把后端回的值合并回这一行（不做整表重刷，行号不跳） */
async function patch(row: AdminUser, patch: Parameters<typeof api.patchUser>[1], okText: string) {
  busyId.value = row.id
  try {
    const out = await api.patchUser(row.id, patch)
    Object.assign(row, {
      display_name: out.display_name,
      status: out.status,
      role: out.role,
    })
    drafts.value[row.id] = out.display_name
    toast(okText)
    if (isMyself(row) && patch.display_name !== undefined) emit('self-renamed', out.display_name)
  } catch (err) {
    const message = err instanceof ApiError ? err.message : '改失败'
    toast(message, 2600)
    // 失败要把控件退回去：否则界面显示的是"已改"，库还是原样
    await load()
  } finally {
    busyId.value = ''
  }
}

function onRoleChange(row: AdminUser, event: Event) {
  const select = event.target as HTMLSelectElement
  const role = select.value as 'user' | 'admin'
  if (role === row.role) return
  if (isMyself(row) && role !== 'admin') {
    select.value = row.role
    toast('不能取消自己的管理员权限 —— 那样就没人能进后台了', 3200)
    return
  }
  void patch(row, { role }, `已把 ${row.username} 设为${role === 'admin' ? '管理员' : '普通用户'}`)
}

function onStatusChange(row: AdminUser, event: Event) {
  const select = event.target as HTMLSelectElement
  const status = select.value as 'active' | 'disabled'
  if (status === row.status) return
  if (isMyself(row) && status === 'disabled') {
    select.value = row.status
    toast('不能停用自己正在用的账号', 3200)
    return
  }
  void patch(row, { status }, `已${status === 'active' ? '启用' : '停用'} ${row.username}`)
}

function saveName(row: AdminUser) {
  const next = (drafts.value[row.id] ?? '').trim()
  if (next === row.display_name) return
  if (next.length > 64) {
    toast('显示名最多 64 个字', 2600)
    return
  }
  void patch(row, { display_name: next }, '显示名已保存')
}

const hasDraft = (row: AdminUser) => (drafts.value[row.id] ?? '') !== row.display_name
</script>

<template>
  <div class="pn-page">
    <header class="ef-head">
      <h1>用户 <small>Accounts</small></h1>
      <p>搜账号、改角色与状态、改显示名。改动立刻生效，不需要重启后端。</p>
    </header>

    <div class="pn-toolbar">
      <label class="pn-field grow">
        <span>搜索用户名 / 显示名</span>
        <input
          v-model="query"
          class="pn-input"
          type="search"
          placeholder="留空显示全部"
          @keydown.enter.prevent="search"
        />
      </label>
      <button class="ef-btn small" type="button" @click="search">搜索</button>
      <button v-if="appliedQuery" class="ef-btn small" type="button" @click="clearSearch">清空</button>
      <label class="pn-field push" style="width: auto">
        <span>每页</span>
        <select class="pn-select inline" :value="limit" @change="changeLimit">
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
      </label>
    </div>

    <StateBlock v-if="loading" kind="loading" />
    <StateBlock v-else-if="error" kind="error" :text="error" @retry="load" />

    <StateBlock
      v-else-if="!rows.length"
      kind="empty"
      :title="appliedQuery ? '没有匹配的账号' : '还没有账号'"
      :text="
        appliedQuery
          ? `「${appliedQuery}」在用户名和显示名里都没有命中，换个词试试。`
          : '第一批用户在客户端注册之后会出现在这里。'
      "
    />

    <template v-else>
      <div class="pn-table-wrap">
        <table class="ef-table pn-table">
          <thead>
            <tr>
              <th class="narrow">#</th>
              <th>用户名</th>
              <th>显示名</th>
              <th class="narrow">角色</th>
              <th class="narrow">状态</th>
              <th class="num narrow">设备</th>
              <th class="narrow">最后活跃</th>
              <th class="narrow">注册</th>
              <th class="narrow">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in rows" :key="row.id" :class="{ 'is-off': row.status === 'disabled' }">
              <td class="narrow mono">{{ fmtInt(offset + index + 1) }}</td>
              <td>
                {{ row.username }}
                <span v-if="isMyself(row)" class="ef-state is-alert">我</span>
              </td>
              <td>
                <input
                  v-model="drafts[row.id]"
                  class="pn-input"
                  style="min-width: 9rem"
                  maxlength="64"
                  :disabled="busyId === row.id"
                  @keydown.enter.prevent="saveName(row)"
                />
              </td>
              <td class="narrow">
                <select
                  class="pn-select inline"
                  :value="row.role"
                  :disabled="busyId === row.id || isMyself(row)"
                  :title="isMyself(row) ? '不能改自己的角色' : '改角色立刻生效'"
                  @change="onRoleChange(row, $event)"
                >
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
              </td>
              <td class="narrow">
                <select
                  class="pn-select inline"
                  :value="row.status"
                  :disabled="busyId === row.id || isMyself(row)"
                  :title="isMyself(row) ? '不能停用自己' : '停用后该账号无法登录'"
                  @change="onStatusChange(row, $event)"
                >
                  <option value="active">active</option>
                  <option value="disabled">disabled</option>
                </select>
              </td>
              <td class="num narrow">{{ fmtInt(row.devices) }}</td>
              <td class="narrow">{{ fmtDateTime(row.last_seen_at) }}</td>
              <td class="narrow">{{ fmtDate(row.created_at) }}</td>
              <td class="narrow">
                <button
                  class="ef-btn small"
                  type="button"
                  :disabled="!hasDraft(row) || busyId === row.id"
                  :aria-busy="busyId === row.id"
                  @click="saveName(row)"
                >
                  保存
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pn-pager">
        <button class="ef-btn small" type="button" :disabled="offset === 0" @click="goPage(-1)">上一页</button>
        <span>第 <b>{{ page }}</b> / {{ pages }} 页</span>
        <button class="ef-btn small" type="button" :disabled="offset + limit >= total" @click="goPage(1)">
          下一页
        </button>
        <span style="margin-left: auto">
          共 <b>{{ fmtInt(total) }}</b> 个账号 · 本页 {{ rows.length }} 行
          <template v-if="appliedQuery"> · 搜索「{{ appliedQuery }}」</template>
        </span>
      </div>

      <p class="pn-note pn-mt">
        角色决定能不能进这个面板；状态 disabled 的账号客户端也登不进去。
        带 <span class="ef-state is-alert">我</span> 的那行是当前登录账号，角色和状态在这里锁住了。
      </p>
    </template>
  </div>
</template>
