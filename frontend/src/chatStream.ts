export type ChatEvent = {type: string; data: any}
type ChatBody = {request_id?: string; [key: string]: unknown}

export class ChatTransportError extends Error {}
class ChatServerError extends Error {}

export function requestId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 15) | 64
  bytes[8] = (bytes[8]! & 63) | 128
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

async function pause(signal: AbortSignal) {
  await new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(signal.reason); return }
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 750)
    signal.addEventListener('abort', abort, {once: true})
  })
}

async function consume(path: string, body: ChatBody, onEvent: (event: ChatEvent) => void, signal: AbortSignal) {
  const response = await fetch('/api' + path, {
    method: 'POST', credentials: 'same-origin', signal,
    headers: {'X-Requested-With': 'calculus-studio', 'Content-Type': 'application/json', Accept: 'text/event-stream'},
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    // Proxy/server errors can arrive after the server has accepted the request.
    const ErrorType = response.status >= 500 ? ChatTransportError : ChatServerError
    throw new ErrorType(typeof data.detail === 'string' ? data.detail : '无法开始回答，请重试')
  }
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) {
    throw new ChatTransportError('服务未返回流式回答')
  }
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const {value, done} = await reader.read()
      buffer += decoder.decode(value, {stream: !done})
      let boundary: RegExpExecArray | null
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        let type = 'message'
        const lines: string[] = []
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith('event:')) type = line.slice(6).trim()
          if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''))
        }
        if (!lines.length) continue
        const data = JSON.parse(lines.join('\n'))
        if (type === 'error') throw new ChatServerError(data.message || '回答未能完成，请重试')
        onEvent({type, data})
        if (type === 'done') return
      }
      if (done) throw new ChatTransportError('连接已中断')
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

async function recover(path: string, id: string, onEvent: (event: ChatEvent) => void, signal: AbortSignal) {
  onEvent({type: 'reset', data: {recovery: true}})
  onEvent({type: 'status', data: {message: '连接中断，正在检查这次回答的进度…'}})
  const statusPath = '/api' + path.replace(/\/stream$/, '') + '/requests/' + encodeURIComponent(id)
  for (let attempt = 0; attempt < 20; attempt++) {
    signal.throwIfAborted()
    let response: Response
    try { response = await fetch(statusPath, {credentials: 'same-origin', signal}) }
    catch (error) { if (signal.aborted) throw error; await pause(signal); continue }
    if (response.status === 404) return false // Resubmit the same ID, never create a second run.
    const run = await response.json().catch(() => ({}))
    if (!response.ok) {
      if (response.status >= 500) { await pause(signal); continue }
      throw new ChatServerError(typeof run.detail === 'string' ? run.detail : '无法读取这次回答')
    }
    if (run.state === 'succeeded' && run.result) { onEvent({type: 'done', data: run.result}); return true }
    if (run.state === 'failed' || run.state === 'cancelled') {
      throw new ChatServerError(run.error?.message || '这次回答未完成，可重新发送问题。')
    }
    onEvent({type: 'status', data: {message: run.state === 'queued' ? '这次回答仍在排队…' : '这次回答仍在处理，正在等待结果…'}})
    await pause(signal)
  }
  throw new ChatTransportError('暂时无法取回结果，可点击“检查这次回答”继续查询。')
}

export async function streamChat(path: string, body: ChatBody, onEvent: (event: ChatEvent) => void, signal: AbortSignal) {
  const request = {...body, request_id: body.request_id || requestId()}
  for (let attempt = 0; attempt < 2; attempt++) {
    try { await consume(path, request, onEvent, signal); return }
    catch (error) {
      if (signal.aborted || error instanceof ChatServerError) throw error
      if (await recover(path, request.request_id, onEvent, signal)) return
    }
  }
  throw new ChatTransportError('连接暂时不可用，可点击“检查这次回答”继续查询。')
}
