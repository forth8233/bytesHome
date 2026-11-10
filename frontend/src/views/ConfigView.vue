<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NSelect, NSpace, NSwitch, NTag, NDynamicTags, useDialog, useMessage } from 'naive-ui'

import api, { errorMessage } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const message = useMessage()
const dialog = useDialog()
const isAdmin = computed(() => auth.isAdmin)

const tab = ref('ha')
const saving = ref(false)

const ha = reactive({ base_url: '', token: '', test: true, connected: false, token_set: false })
const llm = reactive({
  provider: 'deepseek',
  model_name: '',
  api_base: '',
  api_key: '',
  temperature: 0.1,
  timeout: 60,
  max_tokens: 1024,
  fallback_provider: null,
  fallback_model: '',
  fallback_api_base: '',
  fallback_api_key: '',
})
const providers = ref({})
const llmAvailable = ref(true)

const globalForm = reactive({
  active_reasoning_enabled: true,
  proposal_timeout_seconds: 900,
  proposal_dedupe_seconds: 600,
  feedback_cooldown_seconds: 3600,
  feedback_reject_threshold: 2,
  notify_min_interval_seconds: 300,
  llm_rate_limit_per_minute: 30,
  debounce_seconds: '{}',
  storm_limit: '{}',
  retention: '{}',
  fast_path_enabled: true,
  fast_path_state_max_age_seconds: 60,
  fast_path_session_entity_ttl_seconds: 120,
  fast_path_max_candidates: 3,
  fast_path_member_rate_limit_per_minute: 30,
  voice_enabled: true,
  voice_max_group_actions: 50,
  voice_default_steps: '{}',
  voice_terminal_floor_id: null,
  voice_terminal_area_id: null,
})

const fpRules = ref([])
const fpAliases = ref([])
const fpStats = ref(null)
const fpTestText = ref('关客厅灯')
const fpTestResult = ref(null)
const fpTesting = ref(false)
const ruleModal = ref(false)
const aliasModal = ref(false)
const ruleForm = reactive({
  id: null,
  name: '',
  priority: 100,
  enabled: true,
  phrasesText: '',
  actionsText: '',
  reply_template: '',
})
const aliasForm = reactive({
  id: null,
  entity_id: '',
  alias: '',
  display_name: '',
  area_id: '',
  can_control: true,
})

const LEXICON_LABELS = {
  floorName: '楼层名称',
  areaName: '区域名称',
  deviceName: '设备名称',
  sceneName: '场景名称',
  function: '功能名称',
  enum: '枚举值',
  value: '属性数值',
  max: '最大值',
  min: '最小值',
  increase: '相对调高',
  decrease: '相对调低',
  query: '查询',
  little: '一点',
  unit: '单位',
  deviceType: '设备类型',
}

const voiceLexicons = ref([])
const voiceEntries = ref([])
const voiceTotal = ref(0)
const voicePage = ref(1)
const voicePageSize = 20
const voiceFilters = reactive({ lexicon: null, keyword: '', enabled: null })
const voiceEntriesLoading = ref(false)
const voiceTestText = ref('客厅灯亮度调到50%')
const voiceTestResult = ref(null)
const voiceTesting = ref(false)
const voiceImporting = ref(false)
const voiceImportResult = ref(null)
const voiceProfiles = ref([])
const voiceEntryModal = ref(false)
const voiceEntryForm = reactive({
  id: null, // 编辑时为组内第一个词的 id（仅用于判定系统/自定义与锁定词库）
  entryIds: [],
  lexicon: 'value',
  entryType: 2,
  term: '',
  origTerm: '', // 编辑时的原词条名，改名时作为组定位键
  alias: '',
  aliases: [], // 编辑时整组别名，保存时整体替换
  canonical: '',
  functionsText: '',
  unit: '',
  numericValue: null,
})
const voiceProfileModal = ref(false)
const voiceProfileForm = reactive({
  entity_id: '',
  device_type: '',
  functionsText: '{}',
  stepsText: '{}',
  rangesText: '{}',
  enabled: true,
})
const voiceDirectory = ref({ floors: [], areas: [], entities: [] })
const voiceGroupLists = ref([])
const voiceGroupModal = ref(false)
const voiceGroupForm = reactive({
  id: null,
  name: '',
  device_type: null,
  floor_id: null,
  area_id: null,
  entity_ids: [],
  enabled: true,
})

const VOICE_DEVICE_TYPES = {
  light: '灯光',
  AC: '空调',
  heating: '地暖',
  freshAir: '新风',
  curtain: '窗帘',
  window: '窗户',
  media: '多媒体',
  tv: '电视',
  humidifier: '加湿器',
  dehumidifier: '除湿机',
  airPurifier: '空气净化器',
  smartLock: '智能锁',
  switch: '开关模块',
  cleaning: '清洁设备',
  envSensor: '环境传感',
  secuSensor: '安防传感',
  fireSensor: '消防传感',
}

const whitelistText = ref('')
const members = ref([])
const memberModal = ref(false)
const memberForm = reactive({
  id: null,
  username: '',
  password: '',
  display_name: '',
  role: 'member',
  notify_service: '',
  enabled: true,
})

// 成员设备权限：普通成员默认什么都看不到，必须在这里显式授权
const permissionModal = ref(false)
const permissionMember = ref(null)
const permissionRows = ref([])
const permissionSaving = ref(false)
const permissionEntityOptions = computed(() =>
  voiceDirectory.value.entities.map((item) => ({
    label: `${item.name}（${item.entity_id}）`,
    value: item.entity_id,
  })),
)

const providerOptions = computed(() =>
  Object.entries(providers.value).map(([value, meta]) => ({
    label: meta.label || value,
    value,
  })),
)

// ---------- 消息渠道 ----------
// 多飞书实例：每行一个自建应用，实例名跟随行保存（feishu / feishu_2 …），
// 渠道绑定和推送都按实例名路由，增删应用不会让已绑定的账号错位
const feishuInstances = ref([])
const channelStatus = ref(null)
const channelIdentities = ref([])
const identityModal = ref(false)
const identityForm = reactive({
  id: null,
  channel: 'feishu',
  external_id: '',
  member_id: null,
  display_name: '',
  chat_id: '',
  enabled: true,
})

const feishuRuntime = computed(() => {
  const list = channelStatus.value?.channels || []
  return (name) => list.find((item) => item.name === name) || null
})
// 绑定弹窗的渠道下拉：列出已配置的飞书应用实例（后端只放行这些名字）
const feishuChannelOptions = computed(() =>
  (channelStatus.value?.feishu?.instances || []).map((item) => ({
    label: item.remark || item.name,
    value: item.name,
  })),
)
const dispatcherStats = computed(() => channelStatus.value?.dispatcher || null)
// 语音识别：渠道页顶部卡片，密钥回显只给「已保存」标记
const asrForm = reactive({
  enabled: false,
  api_base: '',
  api_key: '',
  api_key_set: false,
  model: 'FunAudioLLM/SenseVoiceSmall',
  language: 'zh',
})
const asrRuntime = computed(() => channelStatus.value?.asr || null)
const asrRuntimeHint = computed(() => {
  const errors = Object.values(asrRuntime.value?.instances || {})
    .map((item) => item.error)
    .filter(Boolean)
  if (errors.length) return `注意：${errors[0]}`
  const stats = Object.values(asrRuntime.value?.instances || {})
  if (stats.some((item) => item.transcribed > 0)) return '语音识别工作正常。'
  return ''
})
// 未绑定账号的最近来信，管理员点一下就能绑定，不用去开发者后台翻 open_id
const pendingAccounts = computed(() => channelStatus.value?.pending || [])
// 「渠道账号 ID」下拉的候选：待绑定账号 + 已绑定账号，省得管理员手抄 open_id
const accountOptions = computed(() => {
  const options = pendingAccounts.value.map((item) => ({
    label: `${item.display_name || '未命名账号'}（${item.external_id}）· 待绑定`,
    value: item.external_id,
  }))
  for (const item of channelIdentities.value) {
    if (pendingAccounts.value.some((p) => p.external_id === item.external_id)) continue
    options.push({
      label: `${item.member_name || item.display_name || '已绑定'}（${item.external_id}）· 已绑定`,
      value: item.external_id,
    })
  }
  return options
})
const memberOptions = computed(() =>
  members.value.map((item) => ({
    label: item.display_name || item.username,
    value: item.id,
  })),
)

async function loadHa() {
  const { data } = await api.get('/config/ha')
  ha.base_url = data.base_url
  ha.connected = data.connected
  ha.token_set = data.token_set
  ha.token = ''
}

async function loadLlm() {
  const [configResponse, providerResponse] = await Promise.all([
    api.get('/config/llm'),
    api.get('/config/llm/providers'),
  ])
  providers.value = providerResponse.data.providers || {}
  const primary = configResponse.data.primary || {}
  Object.assign(llm, {
    provider: primary.provider || 'deepseek',
    model_name: primary.model_name || '',
    api_base: primary.api_base || '',
    api_key: primary.api_key === '***' ? '' : primary.api_key || '',
    temperature: primary.temperature ?? 0.1,
    timeout: primary.timeout ?? 60,
    max_tokens: primary.max_tokens ?? 1024,
    fallback_provider: configResponse.data.fallback?.provider || null,
    fallback_model: configResponse.data.fallback?.model_name || '',
    fallback_api_base: configResponse.data.fallback?.api_base || '',
    fallback_api_key:
      configResponse.data.fallback?.api_key === '***'
        ? ''
        : configResponse.data.fallback?.api_key || '',
  })
}

async function loadGlobal() {
  const { data } = await api.get('/config/global')
  const fastPath = data.fast_path || {}
  Object.assign(globalForm, {
    active_reasoning_enabled: Boolean(data.active_reasoning_enabled),
    proposal_timeout_seconds: data.proposal_timeout_seconds,
    proposal_dedupe_seconds: data.proposal_dedupe_seconds,
    feedback_cooldown_seconds: data.feedback_cooldown_seconds,
    feedback_reject_threshold: data.feedback_reject_threshold,
    notify_min_interval_seconds: data.notify_min_interval_seconds,
    llm_rate_limit_per_minute: data.llm_rate_limit_per_minute,
    debounce_seconds: JSON.stringify(data.debounce_seconds || {}, null, 2),
    storm_limit: JSON.stringify(data.storm_limit || {}, null, 2),
    retention: JSON.stringify(data.retention || {}, null, 2),
    fast_path_enabled: Boolean(fastPath.enabled),
    fast_path_state_max_age_seconds: fastPath.state_max_age_seconds ?? 60,
    fast_path_session_entity_ttl_seconds: fastPath.session_entity_ttl_seconds ?? 120,
    fast_path_max_candidates: fastPath.max_candidates ?? 3,
    fast_path_member_rate_limit_per_minute: fastPath.member_rate_limit_per_minute ?? 30,
    voice_enabled: Boolean(fastPath.voice_enabled ?? true),
    voice_max_group_actions: fastPath.voice_max_group_actions ?? 50,
    voice_default_steps: JSON.stringify(fastPath.voice_default_steps || {}, null, 2),
    voice_terminal_floor_id: fastPath.voice_terminal_floor_id || null,
    voice_terminal_area_id: fastPath.voice_terminal_area_id || null,
  })
}

async function loadFastPath() {
  const [rules, aliases, stats] = await Promise.all([
    api.get('/fastpath/rules'),
    api.get('/fastpath/aliases'),
    api.get('/stats/fastpath'),
  ])
  fpRules.value = rules.data.items || []
  fpAliases.value = aliases.data.items || []
  fpStats.value = stats.data
}

async function loadVoiceLexicons() {
  const { data } = await api.get('/fastpath/voice/lexicons')
  voiceLexicons.value = data.items || []
}

async function loadVoiceEntries() {
  voiceEntriesLoading.value = true
  try {
    const params = { page: voicePage.value, page_size: voicePageSize }
    if (voiceFilters.lexicon) params.lexicon = voiceFilters.lexicon
    if (voiceFilters.keyword.trim()) params.keyword = voiceFilters.keyword.trim()
    if (voiceFilters.enabled !== null) params.enabled = voiceFilters.enabled
    const { data } = await api.get('/fastpath/voice/entries', { params })
    voiceEntries.value = data.items || []
    voiceTotal.value = data.total || 0
  } finally {
    voiceEntriesLoading.value = false
  }
}

async function loadVoiceProfiles() {
  const { data } = await api.get('/fastpath/voice/profiles')
  voiceProfiles.value = data.items || []
}

async function loadVoiceDirectory() {
  const { data } = await api.get('/fastpath/voice/directory')
  voiceDirectory.value = {
    floors: data.floors || [],
    areas: data.areas || [],
    entities: data.entities || [],
  }
}

async function loadVoiceGroupLists() {
  const { data } = await api.get('/fastpath/voice/group-lists')
  voiceGroupLists.value = data.items || []
}

function areaOptionsFor(floorId) {
  return voiceDirectory.value.areas
    .filter((item) => !floorId || item.floor_id === floorId)
    .map((item) => ({ label: item.name, value: item.id }))
}

function onTerminalFloorChange() {
  const area = voiceDirectory.value.areas.find(
    (item) => item.id === globalForm.voice_terminal_area_id,
  )
  if (area && globalForm.voice_terminal_floor_id && area.floor_id !== globalForm.voice_terminal_floor_id) {
    globalForm.voice_terminal_area_id = null
  }
}

function openVoiceGroup(row = null) {
  Object.assign(voiceGroupForm, {
    id: row?.id || null,
    name: row?.name || '',
    device_type: row?.device_type || null,
    floor_id: row?.floor_id || null,
    area_id: row?.area_id || null,
    entity_ids: row?.entity_ids || [],
    enabled: row?.enabled ?? true,
  })
  voiceGroupModal.value = true
}

function onGroupFloorChange() {
  const area = voiceDirectory.value.areas.find(
    (item) => item.id === voiceGroupForm.area_id,
  )
  if (area && voiceGroupForm.floor_id && area.floor_id !== voiceGroupForm.floor_id) {
    voiceGroupForm.area_id = null
  }
}

function onGroupTypeChange() {
  const allowed = new Set(
    voiceDirectory.value.entities
      .filter((item) => item.device_type === voiceGroupForm.device_type)
      .map((item) => item.entity_id),
  )
  voiceGroupForm.entity_ids = (voiceGroupForm.entity_ids || []).filter((id) => allowed.has(id))
}

async function saveVoiceGroup() {
  if (!voiceGroupForm.device_type) {
    message.warning('请先选择设备类型')
    return
  }
  const payload = {
    name: voiceGroupForm.name.trim(),
    device_type: voiceGroupForm.device_type,
    floor_id: voiceGroupForm.floor_id || '',
    area_id: voiceGroupForm.area_id || '',
    entity_ids: voiceGroupForm.entity_ids || [],
    enabled: voiceGroupForm.enabled,
  }
  try {
    if (voiceGroupForm.id) {
      await api.put(`/fastpath/voice/group-lists/${voiceGroupForm.id}`, payload)
    } else {
      await api.post('/fastpath/voice/group-lists', payload)
    }
    voiceGroupModal.value = false
    message.success('群控设备列表已保存，语音已重载')
    await loadVoiceGroupLists()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function removeVoiceGroup(row) {
  dialog.warning({
    title: '删除群控设备列表',
    content: `确认删除「${row.name || row.device_type}」？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/fastpath/voice/group-lists/${row.id}`)
        message.success('已删除，语音已重载')
        await loadVoiceGroupLists()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function loadVoice() {
  const results = await Promise.allSettled([
    loadVoiceLexicons(),
    loadVoiceEntries(),
    loadVoiceProfiles(),
    loadVoiceDirectory(),
    loadVoiceGroupLists(),
    loadFastPath(),
  ])
  for (const result of results) {
    if (result.status === 'rejected') message.error(errorMessage(result.reason))
  }
}

function searchVoiceEntries() {
  voicePage.value = 1
  loadVoiceEntries().catch((error) => message.error(errorMessage(error)))
}

function onVoicePageChange(page) {
  voicePage.value = page
  loadVoiceEntries().catch((error) => message.error(errorMessage(error)))
}

async function testVoice() {
  if (!voiceTestText.value.trim()) return
  voiceTesting.value = true
  try {
    const { data } = await api.post('/fastpath/voice/test', {
      text: voiceTestText.value.trim(),
    })
    voiceTestResult.value = data
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    voiceTesting.value = false
  }
}

function openVoiceEntry(row = null) {
  const aliases =
    row?.aliases && row.aliases.length ? row.aliases : row?.alias ? [row.alias] : []
  Object.assign(voiceEntryForm, {
    id: row?.id || null,
    entryIds: row?.entry_ids || [],
    lexicon: row?.lexicon || 'value',
    entryType: row?.entry_type ?? 2,
    term: row?.term || '',
    origTerm: row?.term || '',
    alias: '',
    aliases,
    canonical: row?.canonical || '',
    functionsText: (row?.functions || []).join(', '),
    unit: row?.unit || '',
    numericValue: row?.numeric_value ?? null,
  })
  voiceEntryModal.value = true
}

function parseFunctions(text) {
  return text
    .split(/[,，、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseAliases(text) {
  const seen = new Set()
  const out = []
  for (const item of text.split(/[,，、\s]+/)) {
    const alias = item.trim()
    if (alias && !seen.has(alias)) {
      seen.add(alias)
      out.push(alias)
    }
  }
  return out
}

async function saveVoiceEntry() {
  const term = voiceEntryForm.term.trim()
  const mergedAliases = parseAliases(voiceEntryForm.aliases.join(','))
  if (!mergedAliases.length) {
    message.error('请至少填写一个别名')
    return
  }
  // 未填词条名时用第一条别名兜底，与后端 create 语义一致
  const actingTerm = term || mergedAliases[0]
  const payload = {
    lexicon: voiceEntryForm.lexicon,
    term: actingTerm,
    aliases: mergedAliases,
    canonical: voiceEntryForm.canonical.trim(),
    functions: parseFunctions(voiceEntryForm.functionsText),
    unit: voiceEntryForm.unit.trim(),
    numeric_value: voiceEntryForm.numericValue,
  }
  try {
    if (voiceEntryForm.id) {
      // 整组编辑：以原词条定位，整体替换别名集合；仅当显式改名时发送 new_term
      const body = {
        lexicon: voiceEntryForm.lexicon,
        term: voiceEntryForm.origTerm,
        aliases: mergedAliases,
        canonical: payload.canonical,
        functions: payload.functions,
        unit: payload.unit,
        numeric_value: payload.numeric_value,
      }
      if (term && term !== voiceEntryForm.origTerm) {
        body.new_term = actingTerm
      }
      await api.put(`/fastpath/voice/entries/group`, body)
    } else {
      await api.post('/fastpath/voice/entries', {
        ...payload,
        lexicon: voiceEntryForm.lexicon,
      })
    }
    voiceEntryModal.value = false
    message.success('词条已保存，词库已重载')
    voicePage.value = 1
    await loadVoice()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function toggleVoiceEntry(row, enabled) {
  try {
    await api.put(`/fastpath/voice/entries/group`, {
      lexicon: row.lexicon,
      term: row.term,
      enabled,
    })
    row.enabled = enabled
    await Promise.allSettled([loadVoiceLexicons(), loadFastPath()])
  } catch (error) {
    row.enabled = !enabled
    message.error(errorMessage(error))
  }
}

function removeVoiceEntry(row) {
  const aliases = row.aliases && row.aliases.length ? row.aliases : [row.alias]
  dialog.warning({
    title: '删除自定义词条',
    content: `确认删除整组「${row.term}」及其 ${aliases.length} 个别名？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/fastpath/voice/entries/group`, {
          data: { lexicon: row.lexicon, term: row.term },
        })
        message.success('已删除，词库已重载')
        await loadVoice()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function importVoice({ file, onFinish, onError }) {
  voiceImporting.value = true
  voiceImportResult.value = null
  try {
    const form = new FormData()
    form.append('file', file.file)
    const { data } = await api.post('/fastpath/voice/import', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    voiceImportResult.value = data
    if (data.imported) {
      message.success(`已导入 ${data.imported} 条词条`)
    } else {
      message.warning(data.message || '文件已导入过，未产生变更')
    }
    voicePage.value = 1
    await loadVoice()
    onFinish()
  } catch (error) {
    message.error(errorMessage(error))
    onError()
  } finally {
    voiceImporting.value = false
  }
}

async function reloadVoice() {
  try {
    await api.post('/fastpath/voice/reload')
    message.success('语音词库已重载')
    await loadVoice()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function openVoiceProfile(row = null) {
  Object.assign(voiceProfileForm, {
    entity_id: row?.entity_id || '',
    device_type: row?.device_type || '',
    functionsText: JSON.stringify(row?.functions || {}, null, 2),
    stepsText: JSON.stringify(row?.steps || {}, null, 2),
    rangesText: JSON.stringify(row?.ranges || {}, null, 2),
    enabled: row?.enabled ?? true,
  })
  voiceProfileModal.value = true
}

async function saveVoiceProfile() {
  let functions
  let steps
  let ranges
  try {
    functions = JSON.parse(voiceProfileForm.functionsText || '{}')
    steps = JSON.parse(voiceProfileForm.stepsText || '{}')
    ranges = JSON.parse(voiceProfileForm.rangesText || '{}')
  } catch {
    message.error('覆盖项必须是合法 JSON 对象')
    return
  }
  const entityId = voiceProfileForm.entity_id.trim()
  if (!entityId) {
    message.error('实体 ID 不能为空')
    return
  }
  try {
    await api.put(`/fastpath/voice/profiles/${encodeURIComponent(entityId)}`, {
      device_type: voiceProfileForm.device_type.trim(),
      functions,
      steps,
      ranges,
      enabled: voiceProfileForm.enabled,
    })
    voiceProfileModal.value = false
    message.success('设备能力覆盖已保存')
    await loadVoiceProfiles()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function removeVoiceProfile(row) {
  dialog.warning({
    title: '删除设备能力覆盖',
    content: `确认删除「${row.entity_id}」的覆盖配置？删除后按 HA 属性自动推导。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/fastpath/voice/profiles/${encodeURIComponent(row.entity_id)}`)
        message.success('已删除')
        await loadVoiceProfiles()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function loadWhitelist() {
  const { data } = await api.get('/config/whitelist')
  whitelistText.value = JSON.stringify(data || {}, null, 2)
}

async function loadMembers() {
  const { data } = await api.get('/members')
  members.value = data
}

async function loadChannels() {
  const [overview, identities] = await Promise.all([
    api.get('/channels'),
    api.get('/channels/identities'),
  ])
  channelStatus.value = overview.data
  channelIdentities.value = identities.data
  // 密钥不回显：留空表示沿用旧值
  feishuInstances.value = (overview.data?.feishu?.instances || []).map((item) => ({
    ...item,
    app_secret: '',
  }))
  // 语音识别配置：密钥同样只给「已保存」标记
  const asr = overview.data?.asr?.config || {}
  Object.assign(asrForm, {
    enabled: Boolean(asr.enabled),
    api_base: asr.api_base || '',
    api_key: '',
    api_key_set: Boolean(asr.api_key),
    model: asr.model || 'FunAudioLLM/SenseVoiceSmall',
    language: asr.language || 'zh',
  })
}

// 保存/重载后，长连接是异步建立的：后端 apply() 返回的瞬间适配器还在「连接中」。
// 只在拉取状态、不动表单（避免把正在编辑的密钥字段清空），轮询几秒直到连上或报错。
async function settleChannelStatus(timeoutMs = 6000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const { data } = await api.get('/channels')
    channelStatus.value = data
    const reachable = data?.feishu?.instances || []
    // 任一飞书实例处于未定态（running 且未连上 且无 error）就继续等
    const pending = reachable.some((it) => it.running && !it.connected && !it.error)
    if (!pending) return
    await new Promise((resolve) => setTimeout(resolve, 800))
  }
}

function addFeishuInstance() {
  feishuInstances.value.push({
    name: '',
    enabled: false,
    app_id: '',
    app_secret: '',
    app_secret_set: false,
    push_enabled: true,
    remark: '',
  })
}

function removeFeishuInstance(index) {
  feishuInstances.value.splice(index, 1)
}

async function saveFeishuChannels() {
  saving.value = true
  try {
    const instances = feishuInstances.value.map((item) => {
      const payload = {
        name: item.name || '',
        enabled: Boolean(item.enabled),
        app_id: item.app_id || '',
        push_enabled: Boolean(item.push_enabled),
        remark: item.remark || '',
      }
      if (item.app_secret) payload.app_secret = item.app_secret
      return payload
    })
    const { data } = await api.put('/channels/feishu', { instances })
    channelStatus.value = data
    message.success('已保存，正在重连长连接')
    await loadChannels()
    await settleChannelStatus()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function reloadChannels() {
  saving.value = true
  try {
    const { data } = await api.post('/channels/feishu/reload')
    channelStatus.value = data
    message.success('已重新加载渠道')
    await loadChannels()
    await settleChannelStatus()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function saveAsrConfig() {
  saving.value = true
  try {
    const payload = {
      enabled: Boolean(asrForm.enabled),
      api_base: asrForm.api_base || '',
      model: asrForm.model || '',
      language: asrForm.language || 'zh',
    }
    if (asrForm.api_key) payload.api_key = asrForm.api_key // 留空沿用旧密钥
    const { data } = await api.put('/channels/asr', payload)
    channelStatus.value = data
    message.success(asrForm.enabled ? '语音识别已启用' : '语音识别已关闭')
    await loadChannels()
    await settleChannelStatus()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

function bindPending(row) {
  // 直接带出 open_id / 会话 ID / 昵称，管理员只需选成员
  openIdentity({
    channel: row.channel,
    external_id: row.external_id,
    display_name: row.display_name || '',
    chat_id: row.chat_id || '',
    enabled: true,
  })
}

function openNewIdentity() {
  // 有未绑定账号就直接带出最近那条，管理员只需选成员
  const latest = pendingAccounts.value[0]
  if (latest) {
    bindPending(latest)
    return
  }
  openIdentity()
}

function openIdentity(row) {
  identityForm.id = row ? row.id : null
  identityForm.channel = row?.channel || 'feishu'
  identityForm.external_id = row?.external_id || ''
  identityForm.member_id = row?.member_id ?? null
  identityForm.display_name = row?.display_name || ''
  identityForm.chat_id = row?.chat_id || ''
  identityForm.enabled = row ? row.enabled : true
  identityModal.value = true
}

async function saveIdentity() {
  if (!identityForm.member_id) {
    message.warning('请先选择绑定成员')
    return
  }
  saving.value = true
  try {
    if (identityForm.id) {
      await api.patch(`/channels/identities/${identityForm.id}`, {
        member_id: identityForm.member_id,
        display_name: identityForm.display_name,
        chat_id: identityForm.chat_id,
        enabled: identityForm.enabled,
      })
    } else {
      await api.post('/channels/identities', {
        channel: identityForm.channel,
        external_id: identityForm.external_id.trim(),
        member_id: identityForm.member_id,
        display_name: identityForm.display_name,
        chat_id: identityForm.chat_id,
        enabled: identityForm.enabled,
      })
    }
    identityModal.value = false
    message.success('已保存绑定')
    await loadChannels()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

function deleteIdentity(row) {
  dialog.warning({
    title: '删除绑定',
    content: `确定删除 ${row.channel} 账号 ${row.external_id} 的绑定吗？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/channels/identities/${row.id}`)
        message.success('已删除')
        await loadChannels()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function loadAll() {
  if (!isAdmin.value) return
  const results = await Promise.allSettled([
    loadHa(),
    loadLlm(),
    loadGlobal(),
    loadWhitelist(),
    loadVoice(),
    loadMembers(),
    loadChannels(),
  ])
  for (const result of results) {
    if (result.status === 'rejected') message.error(errorMessage(result.reason))
  }
}

async function saveHa() {
  saving.value = true
  try {
    const payload = { base_url: ha.base_url, test: ha.test }
    if (ha.token) payload.token = ha.token
    const { data } = await api.post('/config/ha', payload)
    if (data.test) {
      message[data.test.ok ? 'success' : 'error'](data.test.message)
    } else {
      message.success('已保存')
    }
    await loadHa()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

function onProviderChange(value) {
  const meta = providers.value[value]
  if (meta && (!llm.api_base || Object.values(providers.value).some((m) => m.default_base_url === llm.api_base))) {
    llm.api_base = meta.default_base_url || llm.api_base
  }
}

async function saveLlm() {
  saving.value = true
  try {
    const payload = {
      provider: llm.provider,
      model_name: llm.model_name,
      api_base: llm.api_base || null,
      api_key: llm.api_key || null,
      temperature: Number(llm.temperature),
      timeout: Number(llm.timeout),
      max_tokens: Number(llm.max_tokens),
      fallback_provider: llm.fallback_provider || null,
      fallback_model: llm.fallback_model || null,
      fallback_api_base: llm.fallback_api_base || null,
      fallback_api_key: llm.fallback_api_key || null,
    }
    await api.post('/config/llm', payload)
    message.success('LLM 配置已保存')
    await loadLlm()
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function testLlm(which) {
  try {
    // 优先携带表单中当前正在编辑的值进行测试，未填的字段回退到已保存配置
    const payload = {
      which,
      provider: llm.provider || null,
      model_name: llm.model_name || null,
      api_base: llm.api_base || null,
      api_key: llm.api_key || null,
    }
    const { data } = await api.post('/config/llm/test', payload)
    message[data.ok ? 'success' : 'error'](data.message)
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function saveGlobal() {
  saving.value = true
  try {
    const payload = {
      active_reasoning_enabled: globalForm.active_reasoning_enabled,
      proposal_timeout_seconds: Number(globalForm.proposal_timeout_seconds),
      proposal_dedupe_seconds: Number(globalForm.proposal_dedupe_seconds),
      feedback_cooldown_seconds: Number(globalForm.feedback_cooldown_seconds),
      feedback_reject_threshold: Number(globalForm.feedback_reject_threshold),
      notify_min_interval_seconds: Number(globalForm.notify_min_interval_seconds),
      llm_rate_limit_per_minute: Number(globalForm.llm_rate_limit_per_minute),
      debounce_seconds: JSON.parse(globalForm.debounce_seconds || '{}'),
      storm_limit: JSON.parse(globalForm.storm_limit || '{}'),
      retention: JSON.parse(globalForm.retention || '{}'),
      fast_path: {
        enabled: globalForm.fast_path_enabled,
        state_max_age_seconds: Number(globalForm.fast_path_state_max_age_seconds),
        session_entity_ttl_seconds: Number(globalForm.fast_path_session_entity_ttl_seconds),
        max_candidates: Number(globalForm.fast_path_max_candidates),
        member_rate_limit_per_minute: Number(
          globalForm.fast_path_member_rate_limit_per_minute,
        ),
        voice_enabled: globalForm.voice_enabled,
        voice_max_group_actions: Number(globalForm.voice_max_group_actions),
        voice_default_steps: JSON.parse(globalForm.voice_default_steps || '{}'),
        voice_terminal_floor_id: globalForm.voice_terminal_floor_id || '',
        voice_terminal_area_id: globalForm.voice_terminal_area_id || '',
      },
    }
    await api.post('/config/global', payload)
    message.success('全局设置已保存')
  } catch (error) {
    message.error(error instanceof SyntaxError ? '嵌套字段不是合法 JSON' : errorMessage(error))
  } finally {
    saving.value = false
  }
}

async function saveWhitelist() {
  saving.value = true
  try {
    const payload = JSON.parse(whitelistText.value || '{}')
    const { data } = await api.post('/config/whitelist', payload)
    message.success(`已保存，当前可见实体 ${data.visible_entities} 个`)
    await loadWhitelist()
  } catch (error) {
    message.error(error instanceof SyntaxError ? '白名单不是合法 JSON' : errorMessage(error))
  } finally {
    saving.value = false
  }
}

function openMember(member = null) {
  Object.assign(memberForm, {
    id: member?.id || null,
    username: member?.username || '',
    password: '',
    display_name: member?.display_name || '',
    role: member?.role || 'member',
    notify_service: member?.notify_service || '',
    enabled: member?.enabled ?? true,
  })
  memberModal.value = true
}

async function saveMember() {
  try {
    if (memberForm.id) {
      const payload = {
        display_name: memberForm.display_name,
        role: memberForm.role,
        notify_service: memberForm.notify_service || '',
        enabled: memberForm.enabled,
      }
      if (memberForm.password) payload.password = memberForm.password
      await api.put(`/members/${memberForm.id}`, payload)
    } else {
      await api.post('/members', {
        username: memberForm.username,
        password: memberForm.password,
        display_name: memberForm.display_name,
        role: memberForm.role,
        notify_service: memberForm.notify_service || null,
      })
    }
    memberModal.value = false
    message.success('成员已保存')
    await loadMembers()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

async function toggleMember(member, enabled) {
  try {
    await api.put(`/members/${member.id}`, { enabled })
    member.enabled = enabled
  } catch (error) {
    member.enabled = !enabled
    message.error(errorMessage(error))
  }
}

function openDeleteMember(member) {
  dialog.warning({
    title: '删除成员',
    content: `确定要删除成员「${member.display_name || member.username}」吗？此操作不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/members/${member.id}`)
        message.success('成员已删除')
        await loadMembers()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function openPermissions(member) {
  permissionMember.value = member
  permissionRows.value = []
  permissionModal.value = true
  try {
    const { data } = await api.get(`/members/${member.id}/permissions`)
    permissionRows.value = (data || []).map((item) => ({ ...item }))
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function addPermissionRow(pattern = '') {
  permissionRows.value.push({ entity_pattern: pattern, can_view: true, can_control: false })
}

function removePermissionRow(index) {
  permissionRows.value.splice(index, 1)
}

function applyPermissionPreset({ pattern, can_control }) {
  const existing = permissionRows.value.find((row) => row.entity_pattern === pattern)
  if (existing) {
    existing.can_view = true
    existing.can_control = can_control
    return
  }
  permissionRows.value.push({ entity_pattern: pattern, can_view: true, can_control })
}

async function savePermissions() {
  if (!permissionMember.value) return
  const rows = permissionRows.value
    .map((row) => ({
      entity_pattern: String(row.entity_pattern || '').trim(),
      can_view: Boolean(row.can_view),
      can_control: Boolean(row.can_control),
    }))
    .filter((row) => row.entity_pattern)
  if (rows.some((row) => !row.can_view && !row.can_control)) {
    message.warning('有一行既不可查看也不可控制，请勾选至少一项或删除该行')
    return
  }
  permissionSaving.value = true
  try {
    await api.put(`/members/${permissionMember.value.id}/permissions`, { permissions: rows })
    message.success('设备权限已保存')
    permissionModal.value = false
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    permissionSaving.value = false
  }
}

function openRule(row = null) {
  Object.assign(ruleForm, {
    id: row?.id || null,
    name: row?.name || '',
    priority: row?.priority ?? 100,
    enabled: row?.enabled ?? true,
    phrasesText: (row?.phrases || []).join('\n'),
    actionsText: JSON.stringify(row?.actions || [], null, 2),
    reply_template: row?.reply_template || '',
  })
  ruleModal.value = true
}

function parsePhrases(text) {
  return text
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function saveRule() {
  let actions
  try {
    actions = JSON.parse(ruleForm.actionsText || '[]')
  } catch {
    message.error('动作必须是合法 JSON 数组')
    return
  }
  const payload = {
    name: ruleForm.name.trim(),
    priority: Number(ruleForm.priority),
    enabled: ruleForm.enabled,
    phrases: parsePhrases(ruleForm.phrasesText),
    actions,
    reply_template: ruleForm.reply_template,
  }
  if (!payload.name || !payload.phrases.length) {
    message.error('名称和触发词不能为空')
    return
  }
  try {
    if (ruleForm.id) {
      await api.put(`/fastpath/rules/${ruleForm.id}`, payload)
    } else {
      await api.post('/fastpath/rules', payload)
    }
    ruleModal.value = false
    message.success('快捷规则已保存')
    await loadFastPath()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function removeRule(row) {
  dialog.warning({
    title: '删除快捷规则',
    content: `确认删除「${row.name}」？历史版本也会一并删除。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/fastpath/rules/${row.id}`)
        message.success('已删除')
        await loadFastPath()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

function openAlias(row = null) {
  Object.assign(aliasForm, {
    id: row?.id || null,
    entity_id: row?.entity_id || '',
    alias: row?.alias || '',
    display_name: row?.display_name || '',
    area_id: row?.area_id || '',
    can_control: row?.can_control ?? true,
  })
  aliasModal.value = true
}

async function saveAlias() {
  const payload = {
    entity_id: aliasForm.entity_id.trim(),
    alias: aliasForm.alias.trim(),
    display_name: aliasForm.display_name.trim(),
    area_id: aliasForm.area_id.trim() || null,
    can_control: aliasForm.can_control,
  }
  if (!payload.entity_id || !payload.alias) {
    message.error('实体 ID 和别名不能为空')
    return
  }
  try {
    if (aliasForm.id) {
      await api.put(`/fastpath/aliases/${aliasForm.id}`, payload)
    } else {
      await api.post('/fastpath/aliases', payload)
    }
    aliasModal.value = false
    message.success('别名已保存')
    await loadFastPath()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

function removeAlias(row) {
  dialog.warning({
    title: '删除别名',
    content: `确认删除别名「${row.alias}」？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.delete(`/fastpath/aliases/${row.id}`)
        message.success('已删除')
        await loadFastPath()
      } catch (error) {
        message.error(errorMessage(error))
      }
    },
  })
}

async function testFastPath() {
  if (!fpTestText.value.trim()) return
  fpTesting.value = true
  try {
    const { data } = await api.post('/fastpath/test', { text: fpTestText.value.trim() })
    fpTestResult.value = data
  } catch (error) {
    message.error(errorMessage(error))
  } finally {
    fpTesting.value = false
  }
}

const ruleColumns = [
  { title: '名称', key: 'name', minWidth: 140 },
  {
    title: '触发词',
    key: 'phrases',
    minWidth: 200,
    render: (row) => row.phrases.join('、') || '—',
  },
  { title: '优先级', key: 'priority', width: 80 },
  { title: '版本', key: 'version', width: 70 },
  {
    title: '状态',
    key: 'enabled',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.enabled ? 'success' : 'default' },
        { default: () => (row.enabled ? '启用' : '停用') },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { size: 'tiny', onClick: () => openRule(row) }, { default: () => '编辑' }),
          h(
            NButton,
            { size: 'tiny', type: 'error', ghost: true, onClick: () => removeRule(row) },
            { default: () => '删除' },
          ),
        ],
      }),
  },
]

const aliasColumns = [
  { title: '别名', key: 'alias', minWidth: 130 },
  { title: '实体 ID', key: 'entity_id', minWidth: 180 },
  { title: '显示名', key: 'display_name', minWidth: 120 },
  { title: '区域', key: 'area_id', width: 100 },
  {
    title: '可控',
    key: 'can_control',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.can_control ? 'success' : 'default' },
        { default: () => (row.can_control ? '是' : '否') },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { size: 'tiny', onClick: () => openAlias(row) }, { default: () => '编辑' }),
          h(
            NButton,
            { size: 'tiny', type: 'error', ghost: true, onClick: () => removeAlias(row) },
            { default: () => '删除' },
          ),
        ],
      }),
  },
]

const lexiconOptions = computed(() =>
  voiceLexicons.value.map((item) => ({
    label: `${LEXICON_LABELS[item.lexicon] || item.lexicon}（${item.total}）`,
    value: item.lexicon,
  })),
)

const voiceEntryColumns = [
  {
    title: '词库',
    key: 'lexicon',
    width: 110,
    render: (row) => LEXICON_LABELS[row.lexicon] || row.lexicon,
  },
  { title: '词条', key: 'term', minWidth: 120 },
  {
    title: '别名',
    key: 'aliases',
    minWidth: 220,
    render: (row) => {
      const aliases =
        row.aliases && row.aliases.length ? row.aliases : [row.alias].filter(Boolean)
      return h(
        NSpace,
        { size: 4, wrap: true },
        {
          default: () =>
            aliases.map((alias) => h(NTag, { size: 'small' }, { default: () => alias })),
        },
      )
    },
  },
  { title: '标识符', key: 'canonical', minWidth: 120 },
  {
    title: '功能',
    key: 'functions',
    minWidth: 130,
    render: (row) => (row.functions || []).join(', ') || '—',
  },
  {
    title: '来源',
    key: 'source',
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.entry_type === 2 ? 'info' : 'default' },
        { default: () => (row.entry_type === 2 ? '自定义' : row.source) },
      ),
  },
  {
    title: '启用',
    key: 'enabled',
    width: 70,
    render: (row) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        'onUpdate:value': (value) => toggleVoiceEntry(row, value),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(
            NButton,
            { size: 'tiny', onClick: () => openVoiceEntry(row) },
            { default: () => '编辑' },
          ),
          row.entry_type === 2
            ? h(
                NButton,
                {
                  size: 'tiny',
                  type: 'error',
                  ghost: true,
                  onClick: () => removeVoiceEntry(row),
                },
                { default: () => '删除' },
              )
            : null,
        ].filter(Boolean),
      }),
  },
]

const voicePagination = computed(() => ({
  page: voicePage.value,
  pageSize: voicePageSize,
  itemCount: voiceTotal.value,
  prefix: ({ itemCount }) => `共 ${itemCount} 条`,
}))

const voiceFloorOptions = computed(() =>
  voiceDirectory.value.floors.map((item) => ({ label: item.name, value: item.id })),
)

const voiceTerminalAreaOptions = computed(() => areaOptionsFor(globalForm.voice_terminal_floor_id))

const voiceTypeOptions = computed(() =>
  Object.entries(VOICE_DEVICE_TYPES).map(([value, label]) => ({ label, value })),
)

const voiceGroupAreaOptions = computed(() => areaOptionsFor(voiceGroupForm.floor_id))

const voiceGroupEntityOptions = computed(() =>
  voiceDirectory.value.entities
    .filter(
      (item) =>
        (!voiceGroupForm.device_type || item.device_type === voiceGroupForm.device_type) &&
        (!voiceGroupForm.floor_id || item.floor_id === voiceGroupForm.floor_id) &&
        (!voiceGroupForm.area_id || item.area_id === voiceGroupForm.area_id),
    )
    .map((item) => ({ label: `${item.name}（${VOICE_DEVICE_TYPES[item.device_type] || item.device_type || '未知'}）`, value: item.entity_id })),
)

function floorLabel(floorId) {
  const floor = voiceDirectory.value.floors.find((item) => item.id === floorId)
  return floor?.name || '全屋'
}

function areaLabel(areaId) {
  const area = voiceDirectory.value.areas.find((item) => item.id === areaId)
  return area?.name || '全部区域'
}

const voiceGroupColumns = [
  { title: '名称', key: 'name', minWidth: 140, render: (row) => row.name || '—' },
  {
    title: '设备类型',
    key: 'device_type',
    width: 110,
    render: (row) => VOICE_DEVICE_TYPES[row.device_type] || row.device_type,
  },
  {
    title: '楼层',
    key: 'floor_id',
    width: 110,
    render: (row) => (row.floor_id ? floorLabel(row.floor_id) : '不限'),
  },
  {
    title: '区域',
    key: 'area_id',
    width: 120,
    render: (row) => (row.area_id ? areaLabel(row.area_id) : '不限'),
  },
  {
    title: '设备数',
    key: 'entity_ids',
    width: 90,
    render: (row) => (row.entity_ids || []).length,
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.enabled ? 'success' : 'default' },
        { default: () => (row.enabled ? '启用' : '停用') },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(
            NButton,
            { size: 'tiny', onClick: () => openVoiceGroup(row) },
            { default: () => '编辑' },
          ),
          h(
            NButton,
            {
              size: 'tiny',
              type: 'error',
              ghost: true,
              onClick: () => removeVoiceGroup(row),
            },
            { default: () => '删除' },
          ),
        ],
      }),
  },
]

const voiceProfileColumns = [
  { title: '实体 ID', key: 'entity_id', minWidth: 220 },
  { title: '设备类型', key: 'device_type', width: 110, render: (row) => row.device_type || '自动' },
  {
    title: '功能覆盖',
    key: 'functions',
    minWidth: 200,
    render: (row) => {
      const keys = Object.keys(row.functions || {})
      return keys.length ? keys.join(', ') : '—'
    },
  },
  {
    title: '步长',
    key: 'steps',
    minWidth: 160,
    render: (row) => {
      const entries = Object.entries(row.steps || {})
      return entries.length ? entries.map(([k, v]) => `${k}: ${v}`).join(', ') : '—'
    },
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (row) =>
      h(
        NTag,
        { size: 'small', type: row.enabled ? 'success' : 'default' },
        { default: () => (row.enabled ? '启用' : '停用') },
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 130,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(
            NButton,
            { size: 'tiny', onClick: () => openVoiceProfile(row) },
            { default: () => '编辑' },
          ),
          h(
            NButton,
            {
              size: 'tiny',
              type: 'error',
              ghost: true,
              onClick: () => removeVoiceProfile(row),
            },
            { default: () => '删除' },
          ),
        ],
      }),
  },
]

const memberColumns = [
  { title: '#', key: 'id', width: 60 },
  { title: '用户名', key: 'username', width: 140 },
  { title: '显示名', key: 'display_name', width: 140 },
  {
    title: '角色',
    key: 'role',
    width: 90,
    render: (row) => h(NTag, { size: 'small' }, { default: () => row.role }),
  },
  {
    title: '通知服务',
    key: 'notify_service',
    minWidth: 180,
    render: (row) => row.notify_service || '—',
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (row) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        'onUpdate:value': (value) => toggleMember(row, value),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 240,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: 'tiny', onClick: () => openMember(row) }, { default: () => '编辑' }),
          h(
            NButton,
            { size: 'tiny', type: 'error', secondary: true, onClick: () => openDeleteMember(row) },
            { default: () => '删除' },
          ),
          row.role === 'admin'
            ? h(
                NTag,
                { size: 'tiny', type: 'success' },
                { default: () => '全部设备' },
              )
            : h(
                NButton,
                { size: 'tiny', onClick: () => openPermissions(row) },
                { default: () => '设备权限' },
              ),
        ],
      }),
  },
]

const permissionColumns = [
  {
    title: '设备匹配规则',
    key: 'entity_pattern',
    minWidth: 240,
    render: (row) =>
      h(NSelect, {
        value: row.entity_pattern,
        size: 'small',
        filterable: true,
        tag: true,
        clearable: false,
        options: permissionEntityOptions.value,
        placeholder: '选择设备，或直接输入 entity_id / 通配符',
        'onUpdate:value': (value) => {
          row.entity_pattern = value || ''
        },
      }),
  },
  {
    title: '可查看',
    key: 'can_view',
    width: 90,
    render: (row) =>
      h(NSwitch, {
        value: row.can_view,
        size: 'small',
        'onUpdate:value': (value) => {
          row.can_view = value
        },
      }),
  },
  {
    title: '可控制',
    key: 'can_control',
    width: 90,
    render: (row) =>
      h(NSwitch, {
        value: row.can_control,
        size: 'small',
        'onUpdate:value': (value) => {
          row.can_control = value
        },
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 80,
    render: (row) =>
      h(
        NButton,
        { size: 'tiny', quaternary: true, type: 'error', onClick: () => removePermissionRow(permissionRows.value.indexOf(row)) },
        { default: () => '删除' },
      ),
  },
]

const channelTagType = (adapter) => {
  if (!adapter) return 'default'
  if (adapter.connected) return 'success'
  if (adapter.error) return 'error'
  return 'warning'
}

const channelTagText = (adapter) => {
  if (!adapter) return '未启用'
  if (adapter.connected) return '已连接'
  if (adapter.error) return '连接异常'
  return adapter.running ? '连接中' : '未运行'
}

const identityColumns = [
  { title: '#', key: 'id', width: 60 },
  {
    title: '渠道',
    key: 'channel',
    width: 90,
    render: (row) => h(NTag, { size: 'small', type: 'info' }, { default: () => row.channel }),
  },
  {
    title: '渠道账号',
    key: 'external_id',
    minWidth: 200,
    render: (row) => row.external_id,
  },
  { title: '绑定成员', key: 'member_name', minWidth: 120 },
  {
    title: '会话 ID',
    key: 'chat_id',
    minWidth: 180,
    render: (row) => row.chat_id || '—',
  },
  {
    title: '启用',
    key: 'enabled',
    width: 80,
    render: (row) =>
      h(NSwitch, {
        value: row.enabled,
        size: 'small',
        'onUpdate:value': (value) => toggleIdentity(row, value),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 120,
    render: (row) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { size: 'tiny', onClick: () => openIdentity(row) }, { default: () => '编辑' }),
          h(
            NButton,
            { size: 'tiny', type: 'error', onClick: () => deleteIdentity(row) },
            { default: () => '删除' },
          ),
        ],
      }),
  },
]

const pendingColumns = [
  {
    title: '渠道',
    key: 'channel',
    width: 90,
    render: (row) => h(NTag, { size: 'small', type: 'warning' }, { default: () => row.channel }),
  },
  {
    title: '账号',
    key: 'external_id',
    minWidth: 220,
    render: (row) =>
      h('div', [
        h('div', { style: 'font-family: monospace' }, row.external_id),
        h('div', { class: 'muted', style: 'font-size: 12px' }, row.display_name || '未获取到昵称'),
      ]),
  },
  {
    title: '最近来信',
    key: 'last_text',
    minWidth: 200,
    render: (row) =>
      h('div', [
        h('div', row.last_text || '—'),
        h('div', { class: 'muted', style: 'font-size: 12px' }, `${row.count || 0} 条`),
      ]),
  },
  {
    title: '操作',
    key: 'actions',
    width: 100,
    render: (row) =>
      h(
        NButton,
        { size: 'tiny', type: 'primary', onClick: () => bindPending(row) },
        { default: () => '绑定' },
      ),
  },
]

async function toggleIdentity(row, value) {
  try {
    await api.patch(`/channels/identities/${row.id}`, { enabled: value })
    await loadChannels()
  } catch (error) {
    message.error(errorMessage(error))
  }
}

onMounted(loadAll)
</script>

<template>
  <div>
    <h2 class="page-title">配置</h2>
    <p class="page-subtitle">所有配置保存后立即生效；HA 连接变更会触发重连。</p>

    <n-alert v-if="!isAdmin" type="warning" :bordered="false">
      仅管理员可以查看和修改配置。
    </n-alert>

    <div v-else class="config-tabs">
      <n-tabs v-model:value="tab" type="line" animated>
        <n-tab-pane name="ha" tab="Home Assistant">
          <n-card size="small">
            <div class="form-grid">
              <n-form-item label="HA 地址">
                <n-input v-model:value="ha.base_url" placeholder="http://homeassistant.local:8123" />
              </n-form-item>
              <n-form-item label="长期访问令牌">
                <n-input
                  v-model:value="ha.token"
                  type="password"
                  show-password-on="click"
                  :placeholder="ha.token_set ? '已配置，留空保持不变' : '粘贴长期访问令牌'"
                />
              </n-form-item>
            </div>
            <n-space align="center" style="margin-top: 8px">
              <n-tag :type="ha.connected ? 'success' : 'warning'" size="small">
                {{ ha.connected ? '已连接' : '未连接' }}
              </n-tag>
              <n-checkbox v-model:checked="ha.test">保存前测试连接</n-checkbox>
            </n-space>
            <div class="form-actions">
              <n-button type="primary" :loading="saving" @click="saveHa">保存并重连</n-button>
            </div>
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="llm" tab="LLM">
          <n-card size="small" title="主模型">
            <div class="form-grid">
              <n-form-item label="Provider">
                <n-select
                  v-model:value="llm.provider"
                  :options="providerOptions"
                  @update:value="onProviderChange"
                />
              </n-form-item>
              <n-form-item label="模型名">
                <n-input v-model:value="llm.model_name" placeholder="deepseek-chat" />
              </n-form-item>
              <n-form-item label="API Base">
                <n-input v-model:value="llm.api_base" />
              </n-form-item>
              <n-form-item label="API Key">
                <n-input
                  v-model:value="llm.api_key"
                  type="password"
                  show-password-on="click"
                  placeholder="留空保持不变"
                />
              </n-form-item>
              <n-form-item label="Temperature">
                <n-input-number v-model:value="llm.temperature" :min="0" :max="2" :step="0.1" />
              </n-form-item>
              <n-form-item label="超时（秒）">
                <n-input-number v-model:value="llm.timeout" :min="5" :max="300" />
              </n-form-item>
              <n-form-item label="Max Tokens">
                <n-input-number v-model:value="llm.max_tokens" :min="64" :max="8192" />
              </n-form-item>
            </div>
            <n-space style="margin-top: 10px">
              <n-button size="small" @click="testLlm('primary')">测试主模型</n-button>
            </n-space>
          </n-card>

          <n-card size="small" title="备用模型（可选）" style="margin-top: 14px">
            <div class="form-grid">
              <n-form-item label="Provider">
                <n-select
                  v-model:value="llm.fallback_provider"
                  :options="providerOptions"
                  clearable
                  placeholder="不启用"
                />
              </n-form-item>
              <n-form-item label="模型名">
                <n-input v-model:value="llm.fallback_model" :disabled="!llm.fallback_provider" />
              </n-form-item>
              <n-form-item label="API Base">
                <n-input v-model:value="llm.fallback_api_base" :disabled="!llm.fallback_provider" />
              </n-form-item>
              <n-form-item label="API Key">
                <n-input
                  v-model:value="llm.fallback_api_key"
                  type="password"
                  show-password-on="click"
                  :disabled="!llm.fallback_provider"
                  placeholder="留空保持不变"
                />
              </n-form-item>
            </div>
            <n-space style="margin-top: 10px">
              <n-button size="small" :disabled="!llm.fallback_provider" @click="testLlm('fallback')">
                测试备用模型
              </n-button>
            </n-space>
          </n-card>

          <div class="form-actions">
            <n-button type="primary" :loading="saving" @click="saveLlm">保存 LLM 配置</n-button>
            <span class="muted">模型返回的 JSON 会经过校验，失败自动重试一次并切换备用模型。</span>
          </div>
        </n-tab-pane>

        <n-tab-pane name="global" tab="全局">
          <n-card size="small">
            <n-form-item label="启用主动推理">
              <n-switch v-model:value="globalForm.active_reasoning_enabled" />
            </n-form-item>
            <div class="form-grid">
              <n-form-item label="提案超时（秒）">
                <n-input-number v-model:value="globalForm.proposal_timeout_seconds" :min="60" />
              </n-form-item>
              <n-form-item label="提案去重窗口（秒）">
                <n-input-number v-model:value="globalForm.proposal_dedupe_seconds" :min="0" />
              </n-form-item>
              <n-form-item label="反馈冷却（秒）">
                <n-input-number v-model:value="globalForm.feedback_cooldown_seconds" :min="0" />
              </n-form-item>
              <n-form-item label="连续拒绝阈值">
                <n-input-number v-model:value="globalForm.feedback_reject_threshold" :min="1" />
              </n-form-item>
              <n-form-item label="通知最小间隔（秒）">
                <n-input-number v-model:value="globalForm.notify_min_interval_seconds" :min="0" />
              </n-form-item>
              <n-form-item label="LLM 每分钟上限">
                <n-input-number v-model:value="globalForm.llm_rate_limit_per_minute" :min="1" />
              </n-form-item>
            </div>
            <h4 class="section-title">快路径</h4>
            <n-form-item label="启用快路径">
              <n-switch v-model:value="globalForm.fast_path_enabled" />
            </n-form-item>
            <div class="form-grid">
              <n-form-item label="状态新鲜度（秒）">
                <n-input-number v-model:value="globalForm.fast_path_state_max_age_seconds" :min="1" />
              </n-form-item>
              <n-form-item label="会话指代有效期（秒）">
                <n-input-number
                  v-model:value="globalForm.fast_path_session_entity_ttl_seconds"
                  :min="10"
                />
              </n-form-item>
              <n-form-item label="最多候选数">
                <n-input-number
                  v-model:value="globalForm.fast_path_max_candidates"
                  :min="1"
                  :max="5"
                />
              </n-form-item>
              <n-form-item label="成员每分钟上限">
                <n-input-number
                  v-model:value="globalForm.fast_path_member_rate_limit_per_minute"
                  :min="1"
                />
              </n-form-item>
              <n-form-item label="语音技能">
                <n-switch v-model:value="globalForm.voice_enabled" />
              </n-form-item>
              <n-form-item label="群控动作上限">
                <n-input-number v-model:value="globalForm.voice_max_group_actions" :min="1" />
              </n-form-item>
              <n-form-item label="语音终端所在楼层">
                <n-select
                  v-model:value="globalForm.voice_terminal_floor_id"
                  :options="voiceFloorOptions"
                  clearable
                  placeholder="未设置"
                  @update:value="onTerminalFloorChange"
                />
              </n-form-item>
              <n-form-item label="语音终端所在区域">
                <n-select
                  v-model:value="globalForm.voice_terminal_area_id"
                  :options="voiceTerminalAreaOptions"
                  clearable
                  placeholder="未设置"
                />
              </n-form-item>
            </div>
            <p class="muted" style="margin-top: -6px">
              指令未说明楼层或区域时，按语音终端位置补齐；两者都未设置时只按全屋匹配。
            </p>
            <n-form-item label="语音相对调节默认步长（JSON）">
              <n-input
                v-model:value="globalForm.voice_default_steps"
                type="textarea"
                class="mono"
                :rows="5"
              />
            </n-form-item>
            <n-form-item label="防抖 debounce_seconds（JSON）">
              <n-input v-model:value="globalForm.debounce_seconds" type="textarea" class="mono" :rows="4" />
            </n-form-item>
            <n-form-item label="风暴限制 storm_limit（JSON）">
              <n-input v-model:value="globalForm.storm_limit" type="textarea" class="mono" :rows="6" />
            </n-form-item>
            <n-form-item label="数据保留 retention（JSON）">
              <n-input v-model:value="globalForm.retention" type="textarea" class="mono" :rows="5" />
            </n-form-item>
            <div class="form-actions">
              <n-button type="primary" :loading="saving" @click="saveGlobal">保存全局设置</n-button>
            </div>
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="whitelist" tab="白名单">
          <n-card size="small">
            <p class="muted" style="margin-top: 0">
              entities/services 的 allow/deny 使用通配符；param_rules 限制数值范围。保存后立即重同步 HA。
            </p>
            <n-input v-model:value="whitelistText" type="textarea" class="mono" :rows="22" />
            <div class="form-actions">
              <n-button type="primary" :loading="saving" @click="saveWhitelist">保存白名单</n-button>
            </div>
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="fastpath" tab="快捷规则">
          <n-card size="small" title="试跑">
            <n-space align="center">
              <n-input
                v-model:value="fpTestText"
                placeholder="输入一句话，例如：关客厅灯"
                style="max-width: 320px"
                @keydown.enter.exact.prevent="testFastPath"
              />
              <n-button type="primary" size="small" :loading="fpTesting" @click="testFastPath">
                试跑
              </n-button>
            </n-space>
            <pre v-if="fpTestResult" class="mono test-result">{{
              JSON.stringify(fpTestResult, null, 2)
            }}</pre>
            <p class="muted" style="margin-bottom: 0">
              试跑只做解析与安全检查预览，不会执行设备动作。
            </p>
          </n-card>

          <n-card size="small" title="运行指标" style="margin-top: 14px">
            <n-space v-if="fpStats" align="center">
              <n-tag size="small" :type="fpStats.enabled ? 'success' : 'default'">
                {{ fpStats.enabled ? '已启用' : '已停用' }}
              </n-tag>
              <span>规则 {{ fpStats.rules_loaded }} 条</span>
              <span>别名 {{ fpStats.aliases_loaded }} 条</span>
              <span>p50 {{ fpStats.latency_ms?.p50 ?? 0 }} ms</span>
              <span>p95 {{ fpStats.latency_ms?.p95 ?? 0 }} ms</span>
              <n-button size="tiny" @click="loadFastPath">刷新</n-button>
            </n-space>
            <pre v-if="fpStats" class="mono">{{ JSON.stringify(fpStats.counters || {}, null, 2) }}</pre>
          </n-card>

          <n-card size="small" title="场景规则" style="margin-top: 14px">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openRule()">新增规则</n-button>
            </div>
            <n-data-table
              :columns="ruleColumns"
              :data="fpRules"
              :bordered="false"
              :single-line="false"
              size="small"
            />
          </n-card>

          <n-card size="small" title="实体别名" style="margin-top: 14px">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openAlias()">新增别名</n-button>
            </div>
            <n-data-table
              :columns="aliasColumns"
              :data="fpAliases"
              :bordered="false"
              :single-line="false"
              size="small"
            />
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="voice" tab="语音词库">
          <n-card size="small" title="语音试跑">
            <n-space align="center" :wrap="true">
              <n-input
                v-model:value="voiceTestText"
                placeholder="输入一句话，例如：客厅灯亮度调到50%"
                style="max-width: 360px"
                @keydown.enter.exact.prevent="testVoice"
              />
              <n-button type="primary" size="small" :loading="voiceTesting" @click="testVoice">
                解析预览
              </n-button>
              <n-button size="small" @click="reloadVoice">重载词库</n-button>
            </n-space>
            <pre v-if="voiceTestResult" class="mono test-result">{{
              JSON.stringify(voiceTestResult, null, 2)
            }}</pre>
            <p class="muted" style="margin-bottom: 0">
              只做语义解析与安全检查预览，不会执行设备动作；没有明确目标时会交给慢思考。
            </p>
          </n-card>

          <n-card size="small" title="运行指标" style="margin-top: 14px">
            <n-space v-if="fpStats" align="center" :wrap="true">
              <n-tag size="small" :type="fpStats.voice_loaded ? 'success' : 'default'">
                {{ fpStats.voice_loaded ? '词库已加载' : '词库未加载' }}
              </n-tag>
              <span>词条 {{ fpStats.voice_entries ?? 0 }} 条</span>
              <span>命中 {{ fpStats.counters?.voice_hit ?? 0 }}</span>
              <span>澄清 {{ fpStats.counters?.voice_clarify ?? 0 }}</span>
              <span>拒绝 {{ fpStats.counters?.voice_rejected ?? 0 }}</span>
              <span>未识别 {{ fpStats.counters?.voice_none ?? 0 }}</span>
              <span>群控动作 {{ fpStats.counters?.voice_group_actions ?? 0 }}</span>
              <n-button size="tiny" @click="loadFastPath">刷新</n-button>
            </n-space>
            <p v-else class="muted" style="margin: 0">暂无指标。</p>
          </n-card>

          <n-card size="small" title="导入词库" style="margin-top: 14px">
            <n-space align="center" :wrap="true">
              <n-upload
                accept=".xlsx"
                :show-file-list="false"
                :custom-request="importVoice"
                :disabled="voiceImporting"
              >
                <n-button size="small" :loading="voiceImporting">上传 xlsx</n-button>
              </n-upload>
              <span class="muted">仅支持汇总版 xlsx，导入后自动热重载；相同文件不会重复导入。</span>
              <n-button
                tag="a"
                size="small"
                href="/api/fastpath/voice/template"
                target="_blank"
              >下载模板</n-button>
            </n-space>
            <pre v-if="voiceImportResult" class="mono test-result">{{
              JSON.stringify(voiceImportResult, null, 2)
            }}</pre>
          </n-card>

          <n-card size="small" title="词条列表" style="margin-top: 14px">
            <n-space align="center" :wrap="true" style="margin-bottom: 12px">
              <n-select
                v-model:value="voiceFilters.lexicon"
                :options="lexiconOptions"
                clearable
                placeholder="全部词库"
                style="width: 200px"
                @update:value="searchVoiceEntries"
              />
              <n-input
                v-model:value="voiceFilters.keyword"
                placeholder="搜索词条 / 别名 / 标识符"
                clearable
                style="width: 240px"
                @keydown.enter.exact.prevent="searchVoiceEntries"
              />
              <n-select
                v-model:value="voiceFilters.enabled"
                :options="[
                  { label: '全部状态', value: null },
                  { label: '启用', value: true },
                  { label: '停用', value: false },
                ]"
                placeholder="全部状态"
                style="width: 130px"
                @update:value="searchVoiceEntries"
              />
              <n-button size="small" @click="searchVoiceEntries">搜索</n-button>
              <n-button type="primary" size="small" @click="openVoiceEntry()">新增词条</n-button>
            </n-space>
            <n-data-table
              remote
              :columns="voiceEntryColumns"
              :data="voiceEntries"
              :loading="voiceEntriesLoading"
              :pagination="voicePagination"
              :bordered="false"
              :single-line="false"
              size="small"
              @update:page="onVoicePageChange"
            />
          </n-card>

          <n-card size="small" title="设备能力覆盖" style="margin-top: 14px">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openVoiceProfile()">
                新增覆盖
              </n-button>
            </div>
            <n-data-table
              :columns="voiceProfileColumns"
              :data="voiceProfiles"
              :bordered="false"
              :single-line="false"
              size="small"
            />
            <p class="muted" style="margin-bottom: 0">
              没有覆盖的实体按 HA 属性自动推导功能、量程与步长。
            </p>
          </n-card>

          <n-card size="small" title="群控设备列表" style="margin-top: 14px">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openVoiceGroup()">
                新增列表
              </n-button>
            </div>
            <n-data-table
              :columns="voiceGroupColumns"
              :data="voiceGroupLists"
              :bordered="false"
              :single-line="false"
              size="small"
            />
            <p class="muted" style="margin-bottom: 0">
              同一设备类型命中多个列表时，按区域、楼层、全屋的顺序取最具体的一个；列表内设备仍受可见性和可控性约束。
            </p>
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="members" tab="成员">
          <n-card size="small">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openMember()">新增成员</n-button>
            </div>
            <n-data-table
              :columns="memberColumns"
              :data="members"
              :bordered="false"
              :single-line="false"
              size="small"
            />
          </n-card>
        </n-tab-pane>

        <n-tab-pane name="channels" tab="消息渠道">
          <n-card size="small" title="语音识别（飞书语音消息转文字）" style="margin-bottom: 14px">
            <template #header-extra>
              <n-button type="primary" size="small" :loading="saving" @click="saveAsrConfig">
                保存语音识别
              </n-button>
            </template>
            <n-form>
              <div class="form-grid">
                <n-form-item label="启用">
                  <n-switch v-model:value="asrForm.enabled" />
                </n-form-item>
                <n-form-item label="接口地址（OpenAI 兼容 /audio/transcriptions）">
                  <n-input
                    v-model:value="asrForm.api_base"
                    placeholder="例如：https://api.siliconflow.cn/v1"
                  />
                </n-form-item>
              </div>
              <div class="form-grid">
                <n-form-item label="API Key">
                  <n-input
                    v-model:value="asrForm.api_key"
                    type="password"
                    show-password-on="click"
                    :placeholder="asrForm.api_key_set ? '已保存，留空不修改' : '填语音识别服务密钥'"
                  />
                </n-form-item>
                <n-form-item label="模型">
                  <n-input
                    v-model:value="asrForm.model"
                    placeholder="例如：FunAudioLLM/SenseVoiceSmall"
                  />
                </n-form-item>
              </div>
            </n-form>
            <p class="muted" style="margin-bottom: 0">
              <template v-if="asrForm.enabled">
                已启用：在飞书里发语音即可当文字指令用。{{ asrRuntimeHint }}
              </template>
              <template v-else>
                未启用：收到语音会提示仅支持文字。兼容 SiliconFlow、Groq、阿里云百炼等 OpenAI
                风格转写接口，密钥加密保存。
              </template>
            </p>
          </n-card>

          <n-card size="small" title="飞书">
            <template #header-extra>
              <n-space size="small">
                <n-button size="small" @click="addFeishuInstance">新增应用</n-button>
                <n-button type="primary" size="small" :loading="saving" @click="saveFeishuChannels">
                  保存并重连
                </n-button>
                <n-button size="small" :loading="saving" @click="reloadChannels">
                  重新加载
                </n-button>
              </n-space>
            </template>
            <n-alert
              v-if="!feishuInstances.length"
              type="info"
              :bordered="false"
              style="margin-bottom: 12px"
            >
              还没有接入飞书应用。点「新增应用」填入 App ID / App Secret，保存后即可收发消息。
            </n-alert>
            <n-card
              v-for="(instance, index) in feishuInstances"
              :key="index"
              size="small"
              embedded
              style="margin-bottom: 12px"
            >
              <template #header>
                <n-space align="center" size="small">
                  <span>{{ instance.remark || instance.name || `应用 ${index + 1}` }}</span>
                  <n-tag size="small" :type="instance.enabled ? 'info' : 'default'">
                    {{ instance.enabled ? '启用' : '停用' }}
                  </n-tag>
                  <n-tag
                    v-if="instance.enabled"
                    size="small"
                    :type="channelTagType(feishuRuntime(instance.name))"
                  >
                    {{ channelTagText(feishuRuntime(instance.name)) }}
                  </n-tag>
                </n-space>
              </template>
              <template #header-extra>
                <n-button size="tiny" type="error" quaternary @click="removeFeishuInstance(index)">
                  删除
                </n-button>
              </template>
              <n-form>
                <div class="form-grid">
                  <n-form-item label="备注名（应用列表展示用）">
                    <n-input
                      v-model:value="instance.remark"
                      maxlength="64"
                      placeholder="例如：主力飞书"
                    />
                  </n-form-item>
                  <n-form-item label="启用">
                    <n-switch v-model:value="instance.enabled" />
                  </n-form-item>
                </div>
                <div class="form-grid">
                  <n-form-item label="App ID">
                    <n-input v-model:value="instance.app_id" placeholder="cli_xxxxxxxxxxxx" />
                  </n-form-item>
                  <n-form-item label="App Secret">
                    <n-input
                      v-model:value="instance.app_secret"
                      type="password"
                      show-password-on="click"
                      :placeholder="instance.app_secret_set ? '已保存，留空不修改' : '填写应用密钥'"
                    />
                  </n-form-item>
                </div>
                <n-form-item label="允许主动推送（提案 / 告警）">
                  <n-switch v-model:value="instance.push_enabled" />
                </n-form-item>
              </n-form>
              <n-alert
                v-if="instance.enabled && feishuRuntime(instance.name)?.error"
                type="error"
                :bordered="false"
              >
                {{ feishuRuntime(instance.name).error }}
              </n-alert>
            </n-card>
            <p class="muted" style="margin-bottom: 0">
              使用自建应用的长连接（WebSocket）收事件，无需公网 IP。请在飞书开发者后台开启「长连接接收事件」并发布版本；应用需具备 im:message.p2p_msg:readonly、im:message:send_as_bot
              权限。先在飞书里给机器人发一条消息，账号会自动出现在下方待绑定列表。
            </p>
            <div v-if="dispatcherStats" class="form-grid" style="margin-top: 12px">
              <span class="muted">收到 {{ dispatcherStats.received }} 条</span>
              <span class="muted">处理 {{ dispatcherStats.handled }} 条</span>
              <span class="muted">未绑定 {{ dispatcherStats.unbound }} 条</span>
              <span class="muted">失败 {{ dispatcherStats.failed }} 条</span>
              <span class="muted">队列 {{ dispatcherStats.queue_size }}</span>
              <n-button size="tiny" quaternary @click="loadChannels">刷新统计</n-button>
            </div>
          </n-card>

          <n-card v-if="pendingAccounts.length" size="small" title="待绑定账号" style="margin-top: 14px">
            <template #header-extra>
              <n-button size="tiny" @click="loadChannels">刷新</n-button>
            </template>
            <n-alert type="warning" :bordered="false" style="margin-bottom: 12px">
              这些账号发来消息但还没有绑定成员，所以收不到回复。点「绑定」选一位成员即可。
            </n-alert>
            <n-data-table
              :columns="pendingColumns"
              :data="pendingAccounts"
              :bordered="false"
              :single-line="false"
              size="small"
            />
          </n-card>

          <n-card size="small" title="账号绑定" style="margin-top: 14px">
            <div style="margin-bottom: 12px">
              <n-button type="primary" size="small" @click="openNewIdentity()">新增绑定</n-button>
              <span v-if="pendingAccounts.length" class="muted" style="margin-left: 10px">
                已自动带出最近发消息的账号
              </span>
            </div>
            <n-data-table
              :columns="identityColumns"
              :data="channelIdentities"
              :bordered="false"
              :single-line="false"
              size="small"
            />
            <p class="muted" style="margin-bottom: 0">
              渠道账号（飞书 open_id）绑定到成员后，才能通过该渠道下发指令；权限、风控与审计沿用成员本身的设置。
            </p>
          </n-card>
        </n-tab-pane>
      </n-tabs>
    </div>

    <n-modal v-model:show="memberModal" preset="card" style="width: 460px" title="成员">
      <n-form>
        <n-form-item v-if="!memberForm.id" label="用户名">
          <n-input v-model:value="memberForm.username" />
        </n-form-item>
        <n-form-item label="密码">
          <n-input
            v-model:value="memberForm.password"
            type="password"
            show-password-on="click"
            :placeholder="memberForm.id ? '留空不修改' : '至少 8 位'"
          />
        </n-form-item>
        <n-form-item label="显示名">
          <n-input v-model:value="memberForm.display_name" />
        </n-form-item>
        <n-form-item label="角色">
          <n-select
            v-model:value="memberForm.role"
            :options="[
              { label: '成员', value: 'member' },
              { label: '管理员', value: 'admin' },
            ]"
          />
        </n-form-item>
        <n-form-item label="通知服务">
          <n-input v-model:value="memberForm.notify_service" placeholder="notify.mobile_app_xxx" />
        </n-form-item>
        <n-form-item v-if="memberForm.id" label="启用">
          <n-switch v-model:value="memberForm.enabled" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="memberModal = false">取消</n-button>
          <n-button type="primary" @click="saveMember">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal
      v-model:show="permissionModal"
      preset="card"
      style="width: 720px"
      :title="`设备权限 · ${permissionMember?.display_name || permissionMember?.username || ''}`"
    >
      <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
        普通成员默认看不到任何设备，未授权时对话里会说「没有找到设备」。用通配符
        <code>*</code> 表示全部，例如 <code>light.*</code> 表示所有灯。
      </n-alert>
      <n-space style="margin-bottom: 12px">
        <n-button size="small" @click="applyPermissionPreset({ pattern: '*', can_control: false })">
          全部设备可查看
        </n-button>
        <n-button size="small" @click="applyPermissionPreset({ pattern: '*', can_control: true })">
          全部设备可控制
        </n-button>
        <n-button size="small" @click="applyPermissionPreset({ pattern: 'light.*', can_control: true })">
          所有灯可控
        </n-button>
        <n-button size="small" type="primary" @click="addPermissionRow()">新增规则</n-button>
      </n-space>
      <n-data-table
        :columns="permissionColumns"
        :data="permissionRows"
        :bordered="false"
        :single-line="false"
        size="small"
      />
      <p class="muted" style="margin: 10px 0 0">
        只能看到「快捷规则 → 语音词库」里已收录的可控设备；其它设备请直接输入 entity_id。
      </p>
      <template #footer>
        <n-space justify="end">
          <n-button @click="permissionModal = false">取消</n-button>
          <n-button type="primary" :loading="permissionSaving" @click="savePermissions">
            保存
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="identityModal" preset="card" style="width: 480px" title="渠道绑定">
      <n-form>
        <n-form-item label="渠道">
          <n-select
            v-model:value="identityForm.channel"
            :disabled="Boolean(identityForm.id)"
            :options="feishuChannelOptions.length ? feishuChannelOptions : [{ label: '飞书', value: 'feishu' }]"
          />
        </n-form-item>
        <n-form-item label="渠道账号 ID">
          <n-select
            v-model:value="identityForm.external_id"
            :disabled="Boolean(identityForm.id)"
            filterable
            tag
            :options="accountOptions"
            :placeholder="accountOptions.length ? '选择发过消息的账号' : '还没有账号发过消息'"
          />
          <template #feedback>
            <span class="muted">
              列表里是这个应用收到过消息的账号（ou_ 开头）。还没有的话，先在飞书里给机器人发一条消息，再回来点「刷新」。
            </span>
          </template>
        </n-form-item>
        <n-form-item label="绑定成员">
          <n-select
            v-model:value="identityForm.member_id"
            :options="memberOptions"
            filterable
            placeholder="选择家庭成员"
          />
        </n-form-item>
        <n-form-item label="会话 ID（可留空）">
          <n-input
            v-model:value="identityForm.chat_id"
            placeholder="留空则收到第一条消息后自动记录"
          />
        </n-form-item>
        <n-form-item label="显示名（备注）">
          <n-input v-model:value="identityForm.display_name" />
        </n-form-item>
        <n-form-item label="启用">
          <n-switch v-model:value="identityForm.enabled" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="identityModal = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="saveIdentity">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="ruleModal" preset="card" style="width: 560px" title="快捷规则">
      <n-form>
        <n-form-item label="名称">
          <n-input v-model:value="ruleForm.name" placeholder="例如：回家模式" />
        </n-form-item>
        <n-form-item label="触发词（每行一个，也可逗号分隔）">
          <n-input
            v-model:value="ruleForm.phrasesText"
            type="textarea"
            :rows="3"
            placeholder="我回家了&#10;到家了"
          />
        </n-form-item>
        <n-form-item label="动作（JSON 数组）">
          <n-input
            v-model:value="ruleForm.actionsText"
            type="textarea"
            class="mono"
            :rows="9"
            placeholder='[{"domain":"light","service":"turn_on","target":{"type":"domain_all","domain":"light"}}]'
          />
        </n-form-item>
        <n-form-item label="回复模板（可选，支持 {count}）">
          <n-input v-model:value="ruleForm.reply_template" placeholder="已执行回家模式。" />
        </n-form-item>
        <div class="form-grid">
          <n-form-item label="优先级">
            <n-input-number v-model:value="ruleForm.priority" :min="0" :max="1000" />
          </n-form-item>
          <n-form-item label="启用">
            <n-switch v-model:value="ruleForm.enabled" />
          </n-form-item>
        </div>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="ruleModal = false">取消</n-button>
          <n-button type="primary" @click="saveRule">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="aliasModal" preset="card" style="width: 480px" title="实体别名">
      <n-form>
        <n-form-item label="实体 ID">
          <n-input v-model:value="aliasForm.entity_id" placeholder="light.living_room" />
        </n-form-item>
        <n-form-item label="别名">
          <n-input v-model:value="aliasForm.alias" placeholder="客厅主灯" />
        </n-form-item>
        <n-form-item label="显示名">
          <n-input v-model:value="aliasForm.display_name" placeholder="客厅主灯" />
        </n-form-item>
        <n-form-item label="HA 区域 ID（可选）">
          <n-input v-model:value="aliasForm.area_id" placeholder="living_room" />
        </n-form-item>
        <n-form-item label="允许控制">
          <n-switch v-model:value="aliasForm.can_control" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="aliasModal = false">取消</n-button>
          <n-button type="primary" @click="saveAlias">保存</n-button>
        </n-space>
      </template>
    </n-modal>
    <n-modal v-model:show="voiceEntryModal" preset="card" style="width: 520px" title="语音词条">
      <n-form>
        <n-form-item label="词库">
          <n-select
            v-model:value="voiceEntryForm.lexicon"
            :options="lexiconOptions"
            :disabled="Boolean(voiceEntryForm.id)"
          />
        </n-form-item>
        <n-form-item label="词条名">
          <n-input v-model:value="voiceEntryForm.term" />
        </n-form-item>
        <n-form-item
          :label="voiceEntryForm.id ? '别名（可多个）' : '别名'"
        >
          <n-dynamic-tags
            v-model:value="voiceEntryForm.aliases"
            :max="30"
            placeholder="输入后回车添加别名"
          />
        </n-form-item>
        <n-form-item label="标识符（canonical）">
          <n-input v-model:value="voiceEntryForm.canonical" placeholder="brightness / 90% / on" />
        </n-form-item>
        <n-form-item label="推断功能（逗号分隔）">
          <n-input
            v-model:value="voiceEntryForm.functionsText"
            placeholder="brightness, colorTemp"
          />
        </n-form-item>
        <div class="form-grid">
          <n-form-item label="单位">
            <n-input v-model:value="voiceEntryForm.unit" placeholder="% / ° / K / 档" />
          </n-form-item>
          <n-form-item label="数值">
            <n-input-number v-model:value="voiceEntryForm.numericValue" clearable />
          </n-form-item>
        </div>
        <p v-if="voiceEntryForm.id && voiceEntryForm.entryType !== 2" class="muted">
          系统词条只能启停，不能修改字段或删除。
        </p>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="voiceEntryModal = false">取消</n-button>
          <n-button type="primary" @click="saveVoiceEntry">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal
      v-model:show="voiceProfileModal"
      preset="card"
      style="width: 560px"
      title="设备能力覆盖"
    >
      <n-form>
        <n-form-item label="实体 ID">
          <n-input
            v-model:value="voiceProfileForm.entity_id"
            :disabled="Boolean(voiceProfileForm.entity_id)"
            placeholder="light.living_room"
          />
        </n-form-item>
        <n-form-item label="设备类型（留空自动识别）">
          <n-input v-model:value="voiceProfileForm.device_type" placeholder="light / AC / curtain" />
        </n-form-item>
        <n-form-item label="功能覆盖（JSON）">
          <n-input
            v-model:value="voiceProfileForm.functionsText"
            type="textarea"
            class="mono"
            :rows="4"
          />
        </n-form-item>
        <n-form-item label="步长覆盖（JSON）">
          <n-input
            v-model:value="voiceProfileForm.stepsText"
            type="textarea"
            class="mono"
            :rows="4"
          />
        </n-form-item>
        <n-form-item label="量程覆盖（JSON）">
          <n-input
            v-model:value="voiceProfileForm.rangesText"
            type="textarea"
            class="mono"
            :rows="4"
            placeholder='{"brightness": [1, 100]}'
          />
        </n-form-item>
        <n-form-item label="启用">
          <n-switch v-model:value="voiceProfileForm.enabled" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="voiceProfileModal = false">取消</n-button>
          <n-button type="primary" @click="saveVoiceProfile">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal
      v-model:show="voiceGroupModal"
      preset="card"
      style="width: 600px"
      title="群控设备列表"
    >
      <n-form>
        <n-form-item label="列表名称">
          <n-input v-model:value="voiceGroupForm.name" placeholder="例如：客厅主灯组" />
        </n-form-item>
        <n-form-item label="设备类型">
          <n-select
            v-model:value="voiceGroupForm.device_type"
            :options="voiceTypeOptions"
            filterable
            @update:value="onGroupTypeChange"
          />
        </n-form-item>
        <div class="form-grid">
          <n-form-item label="楼层（不限留空）">
            <n-select
              v-model:value="voiceGroupForm.floor_id"
              :options="voiceFloorOptions"
              clearable
              placeholder="不限"
              @update:value="onGroupFloorChange"
            />
          </n-form-item>
          <n-form-item label="区域（不限留空）">
            <n-select
              v-model:value="voiceGroupForm.area_id"
              :options="voiceGroupAreaOptions"
              clearable
              placeholder="不限"
            />
          </n-form-item>
        </div>
        <n-form-item label="设备">
          <n-select
            v-model:value="voiceGroupForm.entity_ids"
            :options="voiceGroupEntityOptions"
            multiple
            filterable
            placeholder="选择参与群控的设备"
          />
        </n-form-item>
        <n-form-item label="启用">
          <n-switch v-model:value="voiceGroupForm.enabled" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="voiceGroupModal = false">取消</n-button>
          <n-button type="primary" @click="saveVoiceGroup">保存</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>
