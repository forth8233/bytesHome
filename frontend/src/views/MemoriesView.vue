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
    preferences.value = prefs.data.map((item) => ({ ...item, draftValue: item.value }))
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
  if (!newKey.value.trim() || !newValue.value.trim()) {
    message.warning('请填写 key 和 value')
    return
  }
  try {
    await api.post('/memories/preference', {
      key: newKey.value.trim(),
      value: newValue.value.trim(),
      member_id: memberId.value,
    })
    newKey.value = ''
    newValue.value = ''
    message.success('已保存')
    loadMemories()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function savePreference(row) {
  if (row.draftValue === row.value) return
  try {
    await api.put(`/memories/preference/${row.id}`, { value: row.draftValue })
    row.value = row.draftValue
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
      <div class="memory-toolbar">
        <n-input v-model:value="newKey" placeholder="key，如 climate.夏季温度" style="width: 250px" />
        <n-input v-model:value="newValue" placeholder="value，如 26" style="width: 220px" />
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
        "
      >
        <code style="min-width: 190px">{{ row.key }}</code>
        <n-input v-model:value="row.draftValue" size="small" style="flex: 1" />
        <n-button
          size="small"
          :disabled="row.draftValue === row.value"
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
