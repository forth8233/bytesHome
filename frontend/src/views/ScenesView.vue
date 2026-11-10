<script setup>
import { h, onMounted, ref } from 'vue'
import { NButton, NSpace, NTag, useDialog, useMessage } from 'naive-ui'

import api, { errorMessage } from '../api'
import { formatTime } from '../utils'

const message = useMessage()
const dialog = useDialog()

const loading = ref(false)
const overview = ref({ ha_scenes: [], scripts: [], automations: [] })
const entities = ref([])

const editorOpen = ref(false)
const editorSaving = ref(false)
const editorYaml = ref('')
const editorConfigId = ref('')
const editorTitle = ref('编辑自动化')

const sceneOpen = ref(false)
const sceneSaving = ref(false)
const sceneId = ref('')
const sceneEntities = ref([])
const sceneEditing = ref(false)

const NEW_AUTOMATION = `alias: 新自动化
trigger:
  - platform: time
    at: "22:00:00"
action:
  - service: light.turn_on
    target:
      entity_id: light.living
`

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/scenes/overview')
    overview.value = data
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    loading.value = false
  }
}

async function loadEntities() {
  try {
    const { data } = await api.get('/fastpath/voice/directory')
    entities.value = data.entities || []
  } catch (error) {
    entities.value = []
  }
}

const entityOptions = () =>
  entities.value.map((item) => ({
    label: `${item.name}（${item.entity_id}）`,
    value: item.entity_id,
  }))

async function activateScene(row) {
  try {
    await api.post(`/scenes/ha/${row.entity_id}/activate`)
    message.success(`已执行场景「${row.name}」`)
    load()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function confirmDeleteScene(row) {
  dialog.warning({
    title: '删除场景',
    content: `将从 Home Assistant 删除场景「${row.name}」，删除后无法恢复。`,
    positiveText: '删除',
    negativeText: '返回',
    onPositiveClick: async () => {
      try {
        await api.delete(`/scenes/ha/${row.entity_id}`)
        message.success('场景已删除')
        load()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function runScript(row) {
  try {
    await api.post(`/scenes/scripts/${row.entity_id}/activate`)
    message.success(`已执行脚本「${row.name}」`)
    load()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function openAutomation(row) {
  try {
    const { data } = await api.get(`/scenes/automations/${row.config_id}`)
    editorConfigId.value = row.config_id
    editorYaml.value = data.yaml
    editorTitle.value = `编辑自动化：${row.name}`
    editorOpen.value = true
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function newAutomation() {
  editorConfigId.value = ''
  editorYaml.value = NEW_AUTOMATION
  editorTitle.value = '新建自动化'
  editorOpen.value = true
}

async function saveAutomation() {
  editorSaving.value = true
  try {
    if (editorConfigId.value) {
      await api.put(`/scenes/automations/${editorConfigId.value}`, {
        yaml: editorYaml.value,
      })
    } else {
      await api.post('/scenes/automations', { yaml: editorYaml.value })
    }
    message.success('已保存到 Home Assistant')
    editorOpen.value = false
    load()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    editorSaving.value = false
  }
}

async function triggerAutomation(row) {
  try {
    await api.post(`/scenes/automations/${row.entity_id}/trigger`)
    message.success(`已触发「${row.name}」`)
    load()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function confirmDeleteAutomation(row) {
  dialog.warning({
    title: '删除自动化',
    content: `将从 Home Assistant 删除自动化「${row.name}」，删除后无法恢复。`,
    positiveText: '删除',
    negativeText: '返回',
    onPositiveClick: async () => {
      try {
        await api.delete(`/scenes/automations/${row.config_id}`)
        message.success('自动化已删除')
        load()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

function openSceneCreator() {
  sceneId.value = ''
  sceneEntities.value = []
  sceneEditing.value = false
  sceneOpen.value = true
}

function openSceneEditor(row) {
  sceneId.value = String(row.entity_id || '').replace(/^scene\./, '')
  sceneEntities.value = row.entities || []
  sceneEditing.value = true
  sceneOpen.value = true
}

async function createScene() {
  if (!sceneId.value.trim()) {
    message.warning('请填写场景标识')
    return
  }
  sceneSaving.value = true
  try {
    await api.post('/scenes/ha', {
      scene_id: sceneId.value.trim(),
      snapshot_entities: sceneEntities.value,
    })
    message.success(sceneEditing.value ? '场景已更新' : '场景已创建')
    sceneOpen.value = false
    load()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    sceneSaving.value = false
  }
}

onMounted(() => {
  load()
  loadEntities()
})

function actionButton(label, type, onClick) {
  return h(NButton, { size: 'tiny', type, onClick }, { default: () => label })
}

const sceneColumns = [
  { title: '名称', key: 'name', minWidth: 140 },
  {
    title: '包含实体',
    key: 'entities',
    minWidth: 240,
    ellipsis: { tooltip: true },
    render: (row) => (row.entities || []).join('、') || '—',
  },
  {
    title: '最近执行',
    key: 'last_changed',
    minWidth: 180,
    render: (row) => formatTime(row.last_changed),
  },
  {
    title: '操作',
    key: 'actions',
    width: 150,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => {
          const buttons = [actionButton('执行', 'primary', () => activateScene(row))]
          if (row.deletable !== false) {
            buttons.push(actionButton('编辑', 'default', () => openSceneEditor(row)))
            buttons.push(actionButton('删除', 'error', () => confirmDeleteScene(row)))
          }
          return buttons
        },
      }),
  },
]

const scriptColumns = [
  { title: '名称', key: 'name', minWidth: 140 },
  { title: '实体', key: 'entity_id', minWidth: 220 },
  {
    title: '操作',
    key: 'actions',
    width: 100,
    render: (row) => actionButton('执行', 'primary', () => runScript(row)),
  },
]

const automationColumns = [
  { title: '名称', key: 'name', minWidth: 140 },
  { title: '配置 ID', key: 'config_id', minWidth: 190 },
  {
    title: '状态',
    key: 'state',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.state === 'on' ? 'success' : 'default' },
        { default: () => (row.state === 'on' ? '启用' : '停用') },
      ),
  },
  {
    title: '最近触发',
    key: 'last_triggered',
    minWidth: 180,
    render: (row) => formatTime(row.last_triggered),
  },
  {
    title: '操作',
    key: 'actions',
    width: 210,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          actionButton('编辑', 'default', () => openAutomation(row)),
          actionButton('触发', 'primary', () => triggerAutomation(row)),
          actionButton('删除', 'error', () => confirmDeleteAutomation(row)),
        ],
      }),
  },
]
</script>

<template>
  <div>
    <h2 class="page-title">场景与自动化</h2>
    <p class="page-subtitle">
      这里管理 Home Assistant 自己的场景、脚本与自动化。自动化用 YAML 原文编辑，保存后立即写回 HA。
    </p>

    <n-space style="margin-bottom: 14px" align="center">
      <n-button :loading="loading" @click="load">刷新</n-button>
    </n-space>

    <n-card size="small" title="HA 场景" style="margin-bottom: 14px">
      <template #header-extra>
        <n-button size="small" type="primary" @click="openSceneCreator">新建场景</n-button>
      </template>
      <n-data-table
        :columns="sceneColumns"
        :data="overview.ha_scenes"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :row-key="(row) => row.entity_id"
        size="small"
      />
    </n-card>

    <n-card size="small" title="脚本" style="margin-bottom: 14px">
      <n-data-table
        :columns="scriptColumns"
        :data="overview.scripts"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :row-key="(row) => row.entity_id"
        size="small"
      />
    </n-card>

    <n-card size="small" title="自动化">
      <template #header-extra>
        <n-button size="small" type="primary" @click="newAutomation">新建自动化</n-button>
      </template>
      <n-data-table
        :columns="automationColumns"
        :data="overview.automations"
        :loading="loading"
        :bordered="false"
        :single-line="false"
        :row-key="(row) => row.entity_id"
        size="small"
      />
    </n-card>

    <n-modal
      v-model:show="editorOpen"
      preset="card"
      style="width: 720px"
      :title="editorTitle"
    >
      <n-input
        v-model:value="editorYaml"
        type="textarea"
        class="mono"
        :autosize="{ minRows: 16, maxRows: 30 }"
        placeholder="alias: 名称&#10;trigger:&#10;  - platform: time&#10;    at: '22:00:00'&#10;action:&#10;  - service: light.turn_on"
      />
      <template #footer>
        <n-space justify="end">
          <n-button @click="editorOpen = false">取消</n-button>
          <n-button type="primary" :loading="editorSaving" @click="saveAutomation">
            保存到 HA
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="sceneOpen" preset="card" style="width: 560px" :title="sceneEditing ? '编辑 HA 场景' : '新建 HA 场景'">
      <n-form label-placement="left" label-width="92">
        <n-form-item label="场景标识">
          <n-input
            v-model:value="sceneId"
            placeholder="英文/数字/下划线，例如 movie_time"
          />
        </n-form-item>
        <n-form-item label="快照实体">
          <n-select
            v-model:value="sceneEntities"
            multiple
            filterable
            :options="entityOptions()"
            placeholder="选择要记录当前状态的设备"
          />
        </n-form-item>
      </n-form>
      <n-alert type="info" :bordered="false" style="margin-top: 4px">
        HA 的场景只能保存设备的当前状态快照，不能自定义动作序列；需要自定义动作请用自动化。
      </n-alert>
      <template #footer>
        <n-space justify="end">
          <n-button @click="sceneOpen = false">取消</n-button>
          <n-button type="primary" :loading="sceneSaving" @click="createScene">
            {{ sceneEditing ? '保存' : '创建' }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
