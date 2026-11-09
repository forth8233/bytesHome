<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { useRouter } from 'vue-router'

import api, { errorMessage } from '../api'
import { formatTime, pretty } from '../utils'

const message = useMessage()
const router = useRouter()

const sessions = ref([])
const currentId = ref(null)
const entries = ref([])
const input = ref('')
const sending = ref(false)
const loadingSession = ref(false)
const listRef = ref(null)

const title = computed(() => {
  const found = sessions.value.find((item) => item.id === currentId.value)
  return found?.title || '新对话'
})

async function loadSessions() {
  try {
    const { data } = await api.get('/chat/sessions')
    sessions.value = data
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function openSession(id) {
  if (sending.value) return
  loadingSession.value = true
  try {
    const { data } = await api.get(`/chat/sessions/${id}/messages`)
    currentId.value = id
    entries.value = data.map((item) => ({
      role: item.role,
      content: item.content,
      tool_calls: item.tool_calls || [],
      route: item.route || null,
      proposal: null,
      draft: null,
      confirmed: false,
    }))
    scrollToBottom()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    loadingSession.value = false
  }
}

function newSession() {
  if (sending.value) return
  currentId.value = null
  entries.value = []
  input.value = ''
}

async function removeSession(item) {
  if (sending.value) {
    message.warning('正在回复中，请稍后再删除')
    return
  }
  try {
    await api.delete(`/chat/sessions/${item.id}`)
    sessions.value = sessions.value.filter((row) => row.id !== item.id)
    if (currentId.value === item.id) newSession()
    message.success('对话已删除')
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  entries.value.push({ role: 'user', content: text })
  input.value = ''
  sending.value = true
  scrollToBottom()
  try {
    const { data } = await api.post('/chat', {
      message: text,
      session_id: currentId.value,
    })
    currentId.value = data.session_id
    entries.value.push({
      role: 'assistant',
      content: data.reply,
      tool_calls: data.tool_results || [],
      route:
        data.route === 'fast_path'
          ? {
              route: 'fast_path',
              route_status: data.route_status,
              latency_ms: data.latency_ms,
            }
          : data.route === 'slow_path'
            ? {
                route: 'slow_path',
                route_status: data.route_status,
                latency_ms: data.latency_ms,
              }
            : null,
      proposal: data.proposal,
      draft: data.draft,
      draftYaml: data.draft?.yaml || '',
      confirmed: false,
    })
    await loadSessions()
    scrollToBottom()
  } catch (error) {
    message.error(errorMessage(error))
    entries.value.push({
      role: 'assistant',
      content: `请求失败：${errorMessage(error)}`,
      tool_calls: [],
      proposal: null,
      draft: null,
      confirmed: false,
    })
  } finally {
    sending.value = false
  }
}

async function confirmDraft(entry) {
  if (!entry.draft || entry.confirmed) return
  entry.confirming = true
  try {
    const { data } = await api.post('/chat/confirm', {
      confirm_token: entry.draft.confirm_token,
      yaml: entry.draftYaml,
    })
    entry.confirmed = true
    entry.confirmResult = data
    message.success(`已写入 Home Assistant：${data.automation_id}`)
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    entry.confirming = false
  }
}

function openProposals() {
  router.push('/proposals')
}

function scrollToBottom() {
  nextTick(() => {
    if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
  })
}

onMounted(loadSessions)
</script>

<template>
  <div class="chat-layout">
    <aside class="session-panel">
      <n-button type="primary" secondary block @click="newSession">新建对话</n-button>
      <div class="session-list">
        <div
          v-for="item in sessions"
          :key="item.id"
          class="session-item"
          :class="{ active: item.id === currentId }"
          @click="openSession(item.id)"
        >
          <div class="session-body">
            <div class="session-title">{{ item.title || '未命名对话' }}</div>
            <div class="session-time">{{ formatTime(item.updated_at) }}</div>
          </div>
          <n-popconfirm
            :show-icon="false"
            positive-text="删除"
            negative-text="取消"
            @positive-click="removeSession(item)"
          >
            <template #trigger>
              <button class="session-delete" title="删除对话" @click.stop>删除</button>
            </template>
            删除后无法恢复，确定删除这个对话吗？
          </n-popconfirm>
        </div>
        <div v-if="!sessions.length" class="muted" style="padding: 8px">还没有对话</div>
      </div>
    </aside>

    <section class="chat-panel">
      <div style="padding: 12px 16px; border-bottom: 1px solid rgba(128, 128, 128, 0.18)">
        <strong>{{ title }}</strong>
      </div>

      <div ref="listRef" class="message-list">
        <div v-if="!entries.length && !loadingSession" class="empty-state">
          可以直接说「客厅灯太亮了」「帮我做一个 22 点关灯的自动化」。<br />
          高风险动作会先转成提案，自动化必须确认后才会写入。
        </div>
        <div
          v-for="(entry, index) in entries"
          :key="index"
          class="message-row"
          :class="entry.role === 'user' ? 'user' : 'assistant'"
        >
          <div class="bubble">
            <div v-if="entry.route" class="route-badge">
              <n-tag
                size="tiny"
                :type="entry.route.route === 'fast_path' ? 'success' : 'default'"
              >
                {{
                  entry.route.route === 'fast_path'
                    ? `快路径 · ${entry.route.latency_ms ?? 0} ms`
                    : entry.route.latency_ms != null
                      ? `慢思考 · ${entry.route.latency_ms} ms`
                      : '慢思考'
                }}
              </n-tag>
              <n-tag v-if="entry.route.route_status === 'clarify'" size="tiny" type="warning">
                待澄清
              </n-tag>
              <n-tag v-if="entry.route.route_status === 'proposal'" size="tiny" type="error">
                待确认
              </n-tag>
              <n-tag v-if="entry.route.route_status === 'denied'" size="tiny" type="error">
                已拦截
              </n-tag>
              <n-tag
                v-if="entry.route.route_status === 'degraded'"
                size="tiny"
                type="error"
              >
                降级
              </n-tag>
              <n-tag
                v-if="entry.route.route_status === 'timeout'"
                size="tiny"
                type="warning"
              >
                超时
              </n-tag>
              <n-tag
                v-if="entry.route.route_status === 'rate_limited'"
                size="tiny"
                type="warning"
              >
                限流
              </n-tag>
              <n-tag v-if="entry.route.route_status === 'error'" size="tiny" type="error">
                出错
              </n-tag>
            </div>
            {{ entry.content }}
            <details v-if="entry.tool_calls && entry.tool_calls.length" class="tool-details">
              <summary>工具调用 {{ entry.tool_calls.length }} 次</summary>
              <pre>{{ pretty(entry.tool_calls) }}</pre>
            </details>

            <n-card
              v-if="entry.proposal"
              size="small"
              style="margin-top: 10px"
              title="待确认提案"
            >
              <div>
                <n-tag size="small" type="error">
                  {{ entry.proposal.risk_level }} 风险
                </n-tag>
                <span style="margin-left: 8px">{{ entry.proposal.entity_id }}</span>
              </div>
              <div class="muted" style="margin-top: 6px">
                {{ entry.proposal.suggestion }}
              </div>
              <n-button size="small" type="warning" style="margin-top: 10px" @click="openProposals">
                去审批
              </n-button>
            </n-card>

            <n-card
              v-if="entry.draft"
              size="small"
              style="margin-top: 10px"
              title="自动化草稿（确认后才写入 HA）"
            >
              <n-input
                v-model:value="entry.draftYaml"
                type="textarea"
                :autosize="{ minRows: 6, maxRows: 16 }"
                class="mono"
                :disabled="entry.confirmed"
              />
              <div style="margin-top: 10px; display: flex; gap: 8px; align-items: center">
                <n-button
                  v-if="!entry.confirmed"
                  size="small"
                  type="primary"
                  :loading="entry.confirming"
                  @click="confirmDraft(entry)"
                >
                  确认并写入 HA
                </n-button>
                <n-tag v-else type="success" size="small">
                  已写入 {{ entry.confirmResult?.automation_id }}
                </n-tag>
                <span v-if="!entry.confirmed" class="muted">
                  有效期至 {{ formatTime(entry.draft.expires_at, false) }}
                </span>
              </div>
            </n-card>
          </div>
        </div>
      </div>

      <div class="composer">
        <n-input
          v-model:value="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 6 }"
          placeholder="告诉管家你想做什么…"
          :disabled="sending"
          @keydown.enter.exact.prevent="send"
        />
        <n-button type="primary" :loading="sending" @click="send">发送</n-button>
      </div>
      <div class="composer-hint">Enter 发送，Shift + Enter 换行</div>
    </section>
  </div>
</template>
