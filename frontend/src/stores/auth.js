import { defineStore } from 'pinia'
import api, { clearAuth, saveAuth, storedUser } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    access: localStorage.getItem('ha_access') || '',
    user: storedUser(),
  }),
  getters: {
    isAuthenticated: (state) => Boolean(state.access),
    isAdmin: (state) => state.user?.role === 'admin',
  },
  actions: {
    async login(username, password) {
      const { data } = await api.post('/auth/login', { username, password })
      this.access = data.access_token
      this.user = data.user
      saveAuth(data)
      return data
    },
    logout() {
      api.post('/auth/logout', {}).catch(() => {})
      this.access = ''
      this.user = null
      clearAuth()
    },
  },
})
