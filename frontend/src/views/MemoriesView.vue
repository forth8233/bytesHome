<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { NTag, useDialog, useMessage } from 'naive-ui'

import api, { errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'
import { formatTime } from '../utils'

const auth = useAuthStore()
const message = useMessage()
const dialog = useDialog()

const members = ref([])
const memberId = ref(auth.user?.id || null)
const preferences = ref([])
const feedback = ref([])
const loading = ref(false)
const newKey = ref('')
const newValue = ref('')
const newValueType = ref('text')
const newValueJson = ref('')

const SOURCE_LABELS = { manual: '手写', llm: '自动', rule: '规则', voice: '语音' }
const SCOPE_LABELS = { member: '本成员', home: '全家', global: '全局' }
const TYPE_LABELS = {
  text: '文本',
  number: '数字',
  number_range: '数值区间',
  boolean: '布尔',
  string_enum: '枚举',
  json: 'JSON',
}
const VALUE_TYPE_OPTIONS = Object.entries(TYPE_LABELS).map(([value, label]) => ({
  label,
  value,
}))
const SOURCE_OPTIONS = Object.entries(SOURCE_LABELS).map(([value, label]) => ({
  label,
  value,
}))
const SCOPE_OPTIONS = Object.entries(SCOPE_LABELS).map(([value, label]) => ({
  label,
  value,
}))

const isAdmin = computed(() => auth.isAdmin)
const memberOptions = computed(() =>
  members.value.map((item) => ({
    label: `${item.display_name || item.username}（${item.role}）`,
    value: item.id,
  })),
)
const selectedMember = computed(() => members.value.find((item) => item.id === memberId.value))

async function loadMembers() {
  if (!isAdmin.value) {
    members.value = [auth.user].filter(Boolean)
    return
  }
  try {
    const { data } = await api.get('/members')
    members.value = data
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function loadMemories() {
  if (!memberId.value) return
  loading.value = true
  try {
    const params = { member_id: memberId.value }
    const [prefs, rows] = await Promise.all([
      api.get('/memories/preference', { params }),
      api.get('/memories/feedback', { params }),
    ])
    preferences.value = prefs.data.map((item) => ({
      ...item,
      draftValue: item.value,
      draftValueType: item.value_type || 'text',
      draftValueJson: item.value_json || '',
    }))
    feedback.value = rows.data
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function reloadAll() {
  loadMemories()
}

async function addPreference() {
  if (!newKey.value.trim()) {
    message.warning('请填写 key')
    return
  }
  const value = newValue.value.trim() || newValueJson.value.trim()
  if (!value) {
    message.warning('请填写 value 或结构化值')
    return
  }
  try {
    await api.post('/memories/preference', {
      key: newKey.value.trim(),
      value,
      value_type: newValueType.value,
      value_json: newValueJson.value.trim() || null,
      member_id: memberId.value,
    })
    newKey.value = ''
    newValue.value = ''
    newValueJson.value = ''
    newValueType.value = 'text'
    message.success('已保存')
    loadMemories()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function savePreference(row) {
  const changed = {
    value: row.draftValue,
    value_type: row.draftValueType,
    value_json: row.draftValueJson.trim() || null,
  }
  const same =
    row.value === changed.value &&
    row.value_type === changed.value_type &&
    (row.value_json || '') === changed.value_json
  if (same) return
  try {
    await api.put(`/memories/preference/${row.id}`, changed)
    Object.assign(row, {
      value: changed.value,
      value_type: changed.value_type,
      value_json: changed.value_json,
    })
    message.success('已更新')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function removePreference(row) {
  dialog.warning({
    title: '删除偏好',
    content: `确定删除「${row.key}」吗？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/memories/preference/${row.id}`)
        message.success('已删除')
        loadMemories()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

function purgeMemories() {
  const name = selectedMember.value?.display_name || selectedMember.value?.username || '该成员'
  dialog.warning({
    title: '清空记忆',
    content: `将清空 ${name} 的全部偏好与反馈记忆，此操作不可恢复。`,
    positiveText: '继续',
    negativeText: '取消',
    onPositiveClick: () => {
      dialog.error({
        title: '再次确认',
        content: '清空后模型将不再记得这些偏好和反馈，确定继续？',
        positiveText: '确认清空',
        negativeText: '返回',
        onPositiveClick: async () => {
          try {
            const { data } = await api.post('/memories/purge', {
              confirm: true,
              member_id: memberId.value,
            })
            message.success(
              `已清空偏好 ${data.purged_preferences} 条、反馈 ${data.purged_feedback} 条`,
            )
            loadMemories()
          } catch (error) {
            message.error(errorMessage(error))
          }
        },
      })
    },
  })
}

const feedbackColumns = [
  { title: '事件类型', key: 'event_type', width: 110 },
  { title: '动作签名', key: 'action_signature', width: 220, ellipsis: { tooltip: true } },
  { title: '最近决定', key: 'decision', width: 100 },
  { title: '次数', key: 'count', width: 70 },
  {
    title: '连续拒绝',
    key: 'consecutive_rejects',
    width: 100,
    render: (row) =>
      h(
        NTag,
        {
          size: 'small',
          type: row.consecutive_rejects >= 2 ? 'error' : 'default',
        },
        { default: () => String(row.consecutive_rejects) },
      ),
  },
  {
    title: '最近触发',
    key: 'last_triggered_at',
    width: 170,
    render: (row) => formatTime(row.last_triggered_at),
  },
]

onMounted(async () => {
  await loadMembers()
  if (!memberId.value && members.value.length) memberId.value = members.value[0].id
  await loadMemories()
})
</script>

<template>
  <div>
    <h2 class="page-title">记忆</h2>
    <p class="page-subtitle">
      偏好是软影响，反馈连续拒绝 2 次会进入冷却。清空操作需要二次确认并写入审计日志。
    </p>

    <div class="memory-toolbar">
      <n-select
        v-if="isAdmin"
        v-model:value="memberId"
        :options="memberOptions"
        style="width: 240px"
        placeholder="选择成员"
        @update:value="reloadAll"
      />
      <n-button type="error" secondary :disabled="!isAdmin" @click="purgeMemories">
        清空该成员记忆
      </n-button>
      <span v-if="!isAdmin" class="muted">仅管理员可以清空记忆</span>
    </div>

    <n-card class="section-card" title="偏好记忆" size="small">
      <div class="memory-toolbar" style="flex-wrap: wrap; gap: 8px">
        <n-input v-model:value="newKey" placeholder="key，如 climate.夏季空调温度" style="width: 240px" />
        <n-input v-model:value="newValue" placeholder="value 说明，如 26 左右" style="width: 180px" />
        <n-select v-model:value="newValueType" :options="VALUE_TYPE_OPTIONS" style="width: 130px" />
        <n-input
          v-model:value="newValueJson"
          placeholder="结构化值，如 26 或 [22,28] 或 true"
          style="width: 220px"
        />
        <n-button type="primary" @click="addPreference">新增</n-button>
      </div>
      <div v-if="!preferences.length" class="muted">暂无偏好记忆</div>
      <div
        v-for="row in preferences"
        :key="row.id"
        style="
          display: flex;
          gap: 10px;
          align-items: center;
          padding: 9px 0;
          border-bottom: 1px solid rgba(128, 128, 128, 0.14);
          flex-wrap: wrap;
        "
      >
        <code style="min-width: 200px">{{ row.key }}</code>
        <n-tag size="small" type="default">{{ TYPE_LABELS[row.value_type] || row.value_type }}</n-tag>
        <n-tag size="small" :type="row.source === 'llm' ? 'info' : 'default'">
          来源:{{ SOURCE_LABELS[row.source] || row.source }}
        </n-tag>
        <n-tag v-if="row.source === 'llm'" size="small" type="warning">
          置信度:{{ row.confidence }}
        </n-tag>
        <n-input v-model:value="row.draftValue" size="small" style="flex: 1; min-width: 180px" />
        <n-select
          v-model:value="row.draftValueType"
          :options="VALUE_TYPE_OPTIONS"
          size="small"
          style="width: 120px"
        />
        <n-input
          v-model:value="row.draftValueJson"
          size="small"
          placeholder="结构化值（可空）"
          style="flex: 1; min-width: 160px"
        />
        <n-button
          size="small"
          :disabled="
            row.value === row.draftValue &&
            row.value_type === row.draftValueType &&
            (row.value_json || '') === (row.draftValueJson || '')
          "
          @click="savePreference(row)"
        >
          保存
        </n-button>
        <n-button size="small" type="error" quaternary @click="removePreference(row)">
          删除
        </n-button>
      </div>
    </n-card>

    <n-card class="section-card" title="反馈记忆" size="small">
      <n-data-table
        :columns="feedbackColumns"
        :data="feedback"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        size="small"
      />
    </n-card>
  </div>
</template>
