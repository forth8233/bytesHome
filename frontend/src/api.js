import axios from 'axios'

const ACCESS_KEY = 'ha_access'
const REFRESH_KEY = 'ha_refresh'
const USER_KEY = 'ha_user'

export function saveAuth(data) {
  localStorage.setItem(ACCESS_KEY, data.access_token)
  localStorage.setItem(REFRESH_KEY, data.refresh_token)
  localStorage.setItem(USER_KEY, JSON.stringify(data.user || null))
}

export function clearAuth() {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
  localStorage.removeItem(USER_KEY)
}

export function storedUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    return null
  }
}

const api = axios.create({ baseURL: '/api', timeout: 120000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(ACCESS_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

let refreshing = null

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config || {}
    const status = error.response?.status
    const skip = String(original.url || '').includes('/auth/')
    if (status !== 401 || original._retried || skip) return Promise.reject(error)

    original._retried = true
    const refreshToken = localStorage.getItem(REFRESH_KEY)
    if (!refreshToken) {
      clearAuth()
      if (location.pathname !== '/login') location.href = '/login'
      return Promise.reject(error)
    }
    refreshing =
      refreshing ||
      axios
        .post('/api/auth/refresh', { refresh_token: refreshToken })
        .then((response) => {
          saveAuth(response.data)
          return response.data.access_token
        })
        .finally(() => {
          refreshing = null
        })
    try {
      const token = await refreshing
      original.headers = original.headers || {}
      original.headers.Authorization = `Bearer ${token}`
      return api(original)
    } catch (refreshError) {
      clearAuth()
      if (location.pathname !== '/login') location.href = '/login'
      return Promise.reject(refreshError)
    }
  },
)

export function errorMessage(error) {
  return error?.response?.data?.message || error?.message || '请求失败'
}

export default api
