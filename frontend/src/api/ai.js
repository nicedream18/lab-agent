import { getToken } from '@/utils/auth'

/**
 * SSE 流式对话。每收到一条 data 事件就调用一次 onEvent。
 */
export async function chatStreamApi(data, onEvent) {
  const baseURL = import.meta.env.VITE_API_BASE_URL || ''
  const token = getToken()
  const res = await fetch(`${baseURL}/api/ai/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(data)
  })

  if (res.status === 401) {
    throw new Error('登录已失效，请重新登录')
  }
  if (!res.ok || !res.body) {
    throw new Error('流式接口请求失败')
  }

  const contentType = res.headers.get('content-type') || ''
  if (!contentType.includes('text/event-stream')) {
    const payload = await res.json().catch(() => null)
    throw new Error(payload?.message || '流式接口返回异常')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const dispatchFrame = (frame) => {
    const data = frame
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trim())
      .join('\n')

    if (!data || data === '[DONE]') return

    let event
    try {
      event = JSON.parse(data)
    } catch (error) {
      if (error instanceof SyntaxError) return
      throw error
    }
    onEvent(event)
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      // 归一化 CRLF，并保留可能被 TCP 拆开的半帧和半个 UTF-8 字符。
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
      const frames = buffer.split('\n\n')
      buffer = frames.pop() || ''
      for (const frame of frames) dispatchFrame(frame)
    }

    buffer += decoder.decode().replace(/\r\n/g, '\n')
    if (buffer.trim()) dispatchFrame(buffer)
  } finally {
    reader.releaseLock()
  }
}
