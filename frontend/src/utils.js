export function parseUtc(value) {
  if (!value) return null
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(value)) return new Date(value)
  return new Date(`${value}Z`)
}

export function formatTime(value, withDate = true) {
  const date = parseUtc(value)
  if (!date || Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', {
    hourCycle: 'h23',
    ...(withDate ? { year: 'numeric', month: '2-digit', day: '2-digit' } : {}),
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function pretty(value) {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
