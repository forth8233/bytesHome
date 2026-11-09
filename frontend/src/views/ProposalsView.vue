<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NSpace, NTag, useDialog, useMessage } from 'naive-ui'

import api, { errorMessage } from '../api'
import { useSSE } from '../composables/useSSE'
import { formatTime, pretty } from '../utils'

const message = useMessage()
const dialog = useDialog()

const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref('pending')
const risk = ref(null)
const loading = ref(false)
const selected = ref(null)
const showDetail = ref(false)

const statusOptions = [
  { label: '全部状态', value: null },
  { label: '待确认', value: 'pending' },
  { label: '已批准', value: 'approved' },
  { label: '执行中', value: 'executing' },
  { label: '已执行', value: 'executed' },
  { label: '失败', value: 'failed' },
  { label: '已拒绝', value: 'rejected' },
  { label: '超时', value: 'timeout' },
  { label: '已取消', value: 'cancelled' },
]

const riskOptions = [
  { label: '全部风险', value: null },
  { label: '高风险', value: 'high' },
  { label: '中风险', value: 'medium' },
  { label: '低风险', value: 'low' },
]

const statusType = {
  pending: 'warning',
  approved: 'info',
  executing: 'info',
  executed: 'success',
  failed: 'error',
  rejected: 'default',
  timeout: 'default',
  cancelled: 'default',
  superseded: 'default',
}

const riskType = { high: 'error', medium: 'warning', low: 'success' }

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (status.value) params.status = status.value
    if (risk.value) params.risk_level = risk.value
    const { data } = await api.get('/proposal', { params })
    items.value = data.items
    total.value = data.total
    if (selected.value) {
      const fresh = data.items.find((item) => item.id === selected.value.id)
      if (fresh) selected.value = fresh
    }
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

function reloadFirstPage() {
  page.value = 1
  load()
}

function openDetail(row) {
  selected.value = row
  showDetail.value = true
}

function confirmAction(row, kind) {
  const labels = {
    approve: '确认执行',
    reject: '拒绝提案',
    cancel: '取消提案',
  }
  const contents = {
    approve: `将立即执行 ${row.entity_id} 的动作，且不会自动重试。`,
    reject: `拒绝后该建议会写入反馈记忆，连续拒绝可能触发冷却。`,
    cancel: `取消后该提案不再可审批。`,
  }
  dialog.warning({
    title: labels[kind],
    content: contents[kind],
    positiveText: labels[kind],
    negativeText: '返回',
    onPositiveClick: async () => {
      try {
        const { data } = await api.post(`/proposal/${row.id}/${kind}`)
        selected.value = data
        message.success(kind === 'approve' ? '已执行' : '已处理')
        load()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

const columns = [
  { title: '#', key: 'id', width: 64 },
  {
    title: '时间',
    key: 'created_at',
    width: 160,
    render: (row) => formatTime(row.created_at),
  },
  { title: '实体', key: 'entity_id', minWidth: 180, ellipsis: { tooltip: true } },
  {
    title: '风险',
    key: 'risk_level',
    width: 82,
    render: (row) =>
      h(NTag, { size: 'small', type: riskType[row.risk_level] || 'default' }, {
        default: () => row.risk_level,
      }),
  },
  {
    title: '状态',
    key: 'status',
    width: 90,
    render: (row) =>
      h(NTag, { size: 'small', type: statusType[row.status] || 'default' }, {
        default: () => row.status,
      }),
  },
  { title: '建议', key: 'suggestion', minWidth: 260, ellipsis: { tooltip: true } },
  {
    title: '操作',
    key: 'actions',
    width: 210,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: 'tiny', onClick: () => openDetail(row) }, { default: () => '详情' }),
          row.status === 'pending'
            ? h(
                NButton,
                {
                  size: 'tiny',
                  type: 'primary',
                  onClick: () => confirmAction(row, 'approve'),
                },
                { default: () => '批准' },
              )
            : null,
          row.status === 'pending'
            ? h(
                NButton,
                {
                  size: 'tiny',
                  type: 'error',
                  onClick: () => confirmAction(row, 'reject'),
                },
                { default: () => '拒绝' },
              )
            : null,
        ],
      }),
  },
]

const { connect } = useSSE({
  'proposal.created': () => load(),
  'proposal.updated': () => load(),
  alert: (payload) => {
    dialog.warning({
      title: '高风险告警',
      content: `${payload.entity_name || payload.entity_id}\n\n${payload.suggestion}`,
      positiveText: '知道了',
    })
    load()
  },
})

onMounted(() => {
  load()
  connect()
})
</script>

<template>
  <div>
    <h2 class="page-title">提案审批</h2>
    <p class="page-subtitle">所有设备动作都经过安全策略校验；高风险动作必须人工确认。</p>

    <n-space style="margin-bottom: 14px" align="center">
      <n-select
        v-model:value="status"
        :options="statusOptions"
        style="width: 140px"
        @update:value="reloadFirstPage"
      />
      <n-select
        v-model:value="risk"
        :options="riskOptions"
        placeholder="全部风险"
        style="width: 140px"
        @update:value="reloadFirstPage"
      />
      <n-button :loading="loading" @click="load">刷新</n-button>
      <span class="muted">共 {{ total }} 条</span>
    </n-space>

    <n-data-table
      :columns="columns"
      :data="items"
      :loading="loading"
      :bordered="false"
      :single-line="false"
      :row-key="(row) => row.id"
      size="small"
    />

    <div style="margin-top: 14px; display: flex; justify-content: flex-end">
      <n-pagination
        v-model:page="page"
        :page-size="pageSize"
        :item-count="total"
        @update:page="load"
      />
    </div>

    <n-drawer v-model:show="showDetail" :width="520">
      <n-drawer-content title="提案详情" closable>
        <template v-if="selected">
          <n-descriptions label-placement="left" :column="1" size="small" bordered>
            <n-descriptions-item label="编号">{{ selected.id }}</n-descriptions-item>
            <n-descriptions-item label="实体">{{ selected.entity_id }}</n-descriptions-item>
            <n-descriptions-item label="状态">{{ selected.status }}</n-descriptions-item>
            <n-descriptions-item label="风险">
              {{ selected.risk_level }}（来源 {{ selected.risk_source }}）
            </n-descriptions-item>
            <n-descriptions-item label="来源">{{ selected.source }}</n-descriptions-item>
            <n-descriptions-item label="创建时间">
              {{ formatTime(selected.created_at) }}
            </n-descriptions-item>
            <n-descriptions-item label="过期时间">
              {{ formatTime(selected.expire_at) }}
            </n-descriptions-item>
            <n-descriptions-item label="推理">
              {{ selected.thought || '—' }}
            </n-descriptions-item>
            <n-descriptions-item label="建议">
              {{ selected.suggestion || '—' }}
            </n-descriptions-item>
            <n-descriptions-item v-if="selected.reject_reason" label="拒绝原因">
              {{ selected.reject_reason }}
            </n-descriptions-item>
          </n-descriptions>

          <h4>动作</h4>
          <pre class="json-view">{{ pretty(selected.actions) }}</pre>
          <template v-if="selected.execution_result">
            <h4>执行结果</h4>
            <pre class="json-view">{{ pretty(selected.execution_result) }}</pre>
          </template>

          <n-space v-if="selected.status === 'pending'" style="margin-top: 16px">
            <n-button type="primary" @click="confirmAction(selected, 'approve')">批准执行</n-button>
            <n-button type="error" @click="confirmAction(selected, 'reject')">拒绝</n-button>
            <n-button @click="confirmAction(selected, 'cancel')">取消</n-button>
          </n-space>
        </template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>
