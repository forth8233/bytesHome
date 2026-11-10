<script setup>
import { h, onMounted, ref } from 'vue'
import {
  NButton,
  NSpace,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'

import api, { errorMessage } from '../api'
import { formatTime } from '../utils'

const message = useMessage()
const dialog = useDialog()

const loading = ref(false)
const tasks = ref([])
const runs = ref([])

const KIND_LABELS = {
  relative: '相对',
  absolute: '绝对',
  fuzzy: '模糊',
  duration: '时长',
}
const RUN_STATUS = {
  ok: 'success',
  failed: 'error',
  skipped: 'default',
}
const TASK_STATUS = {
  pending: 'processing',
  ok: 'success',
  fired: 'success',
  cancelled: 'warning',
  failed: 'error',
}

async function load() {
  loading.value = true
  try {
    const [tasksRes, runsRes] = await Promise.all([
      api.get('/timers'),
      api.get('/timers/runs'),
    ])
    tasks.value = tasksRes.data.items || []
    runs.value = runsRes.data.items || []
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function confirmCancel(row) {
  dialog.warning({
    title: '取消定时',
    content: `将取消定时「${row.summary}」，到期将不再执行。`,
    positiveText: '取消定时',
    negativeText: '返回',
    onPositiveClick: async () => {
      try {
        await api.delete(`/timers/${row.id}`)
        message.success('定时已取消')
        load()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

function actionButton(label, type, onClick) {
  return h(NButton, { size: 'tiny', type, onClick }, { default: () => label })
}

const taskColumns = [
  {
    title: '总结',
    key: 'summary',
    minWidth: 180,
    ellipsis: { tooltip: true },
  },
  {
    title: '类型',
    key: 'kind',
    width: 80,
    render: (row) => KIND_LABELS[row.kind] || row.kind,
  },
  {
    title: '动作',
    key: 'text',
    minWidth: 200,
    ellipsis: { tooltip: true },
  },
  {
    title: '计划执行',
    key: 'run_at',
    minWidth: 170,
    render: (row) => formatTime(row.run_at),
  },
  {
    title: '发起人',
    key: 'username',
    minWidth: 100,
    render: (row) => row.username || '—',
  },
  {
    title: '来源',
    key: 'source_channel',
    width: 90,
    render: (row) => {
      if (row.source_channel && row.source_channel.startsWith('feishu')) {
        return h(
          NTag,
          { size: 'small', type: 'primary' },
          { default: () => '飞书' },
        )
      }
      return '网页'
    },
  },
  {
    title: '状态',
    key: 'status',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: TASK_STATUS[row.status] || 'default' },
        { default: () => row.status },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 90,
    render: (row) =>
      row.status === 'pending'
        ? actionButton('取消', 'error', () => confirmCancel(row))
        : '—',
  },
]

const runColumns = [
  { title: '任务', key: 'task_id', width: 80 },
  {
    title: '动作',
    key: 'text',
    minWidth: 200,
    ellipsis: { tooltip: true },
    render: (row) => row.text || '—',
  },
  {
    title: '状态',
    key: 'status',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: RUN_STATUS[row.status] || 'default' },
        { default: () => row.status },
      ),
  },
  {
    title: '执行时间',
    key: 'fired_at',
    minWidth: 170,
    render: (row) => formatTime(row.fired_at),
  },
  {
    title: '详情',
    key: 'detail',
    minWidth: 220,
    ellipsis: { tooltip: true },
    render: (row) => (row.detail ? String(row.detail).slice(0, 80) : '—'),
  },
]

onMounted(load)
</script>

<template>
  <div>
    <h2 class="page-title">定时任务</h2>
    <p class="page-subtitle">
      查看语音/对话设置的定时指令，可提前取消；下方为定时执行记录，便于审查。
    </p>

    <n-space style="margin-bottom: 14px" align="center">
      <n-button :loading="loading" @click="load">刷新</n-button>
    </n-space>

    <n-card size="small" title="进行中的定时" style="margin-bottom: 14px">
      <n-data-table
        v-if="tasks.length"
        :columns="taskColumns"
        :data="tasks"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :row-key="(row) => row.id"
        size="small"
      />
      <div v-else class="muted">还没有已设置的定时。</div>
    </n-card>

    <n-card size="small" title="执行记录">
      <n-data-table
        v-if="runs.length"
        :columns="runColumns"
        :data="runs"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :row-key="(row) => row.id"
        size="small"
      />
      <div v-else class="muted">暂无执行记录。</div>
    </n-card>
  </div>
</template>