import { onBeforeUnmount } from 'vue'

import api from '../api'

export function useSSE(handlers) {
  let source = null
  let timer = null
  let stopped = false

  async function connect() {
    if (stopped) return
    try {
      const { data } = await api.post('/sse/ticket')
      source = new EventSource(`/api/sse/notify?ticket=${encodeURIComponent(data.ticket)}`)
      for (const [type, handler] of Object.entries(handlers)) {
        source.addEventListener(type, (event) => {
          let payload
          try {
            payload = JSON.parse(event.data)
          } catch {
            return
          }
          handler(payload, event)
        })
      }
      source.onerror = () => {
        if (source) source.close()
        source = null
        if (!stopped) timer = setTimeout(connect, 3000)
      }
    } catch {
      if (!stopped) timer = setTimeout(connect, 5000)
    }
  }

  function close() {
    stopped = true
    clearTimeout(timer)
    if (source) source.close()
    source = null
  }

  onBeforeUnmount(close)

  return { connect, close }
}
