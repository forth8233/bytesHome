import { createRouter, createWebHistory } from 'vue-router'

import ChatView from './views/ChatView.vue'
import ConfigView from './views/ConfigView.vue'
import LoginView from './views/LoginView.vue'
import MemoriesView from './views/MemoriesView.vue'
import ProposalsView from './views/ProposalsView.vue'
import ScenesView from './views/ScenesView.vue'
import TimersView from './views/TimersView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView },
    { path: '/', redirect: '/chat' },
    { path: '/chat', component: ChatView },
    { path: '/proposals', component: ProposalsView },
    { path: '/scenes', component: ScenesView },
    { path: '/timers', component: TimersView },
    { path: '/config', component: ConfigView },
    { path: '/memories', component: MemoriesView },
  ],
})

router.beforeEach((to) => {
  const authenticated = Boolean(localStorage.getItem('ha_access'))
  if (to.path !== '/login' && !authenticated) return '/login'
  if (to.path === '/login' && authenticated) return '/'
  return true
})

export default router
