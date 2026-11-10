<script setup>
import { computed, onMounted, ref } from 'vue'
import { darkTheme } from 'naive-ui'
import { useRoute, useRouter } from 'vue-router'

import api from './api.js'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const dark = ref(localStorage.getItem('ha_theme') === 'dark')

const appVersion = ref('')
onMounted(async () => {
  try {
    const { data } = await api.get('/info')
    appVersion.value = data.version || ''
  } catch {
    // 静默：拿不到版本号就不显示
  }
})

const navItems = [
  { path: '/chat', label: '聊天' },
  { path: '/scenes', label: '场景与自动化' },
  { path: '/timers', label: '定时任务' },
  { path: '/proposals', label: '提案审批' },
  { path: '/config', label: '配置' },
  { path: '/memories', label: '记忆' },
]
const showShell = computed(() => auth.isAuthenticated && route.path !== '/login')

function toggleTheme() {
  dark.value = !dark.value
  localStorage.setItem('ha_theme', dark.value ? 'dark' : 'light')
}

function logout() {
  auth.logout()
  router.replace('/login')
}
</script>

<template>
  <n-config-provider :theme="dark ? darkTheme : null">
    <n-message-provider>
      <n-dialog-provider>
        <div v-if="!showShell" class="plain-shell">
          <router-view />
        </div>
        <n-layout v-else position="absolute">
          <n-layout-header bordered class="app-header">
            <div class="brand">
              家庭管家
              <span v-if="appVersion" class="brand-version">v{{ appVersion }}</span>
            </div>
            <nav class="nav">
              <router-link
                v-for="item in navItems"
                :key="item.path"
                :to="item.path"
                class="nav-link"
                :class="{ active: route.path === item.path }"
              >
                {{ item.label }}
              </router-link>
            </nav>
            <div class="header-right">
              <span class="user-name">{{ auth.user?.display_name || auth.user?.username }}</span>
              <n-button quaternary size="small" @click="toggleTheme">
                {{ dark ? '浅色' : '深色' }}
              </n-button>
              <n-button quaternary size="small" @click="logout">退出</n-button>
            </div>
          </n-layout-header>
          <n-layout-content
            class="app-content"
            :class="{ 'app-content--chat': route.path === '/chat' }"
            :native-scrollbar="false"
          >
            <router-view />
          </n-layout-content>
        </n-layout>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
.brand-version {
  font-size: 12px;
  font-weight: normal;
  opacity: 0.65;
  margin-left: 6px;
}
</style>
