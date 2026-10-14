import { reactive } from 'vue'
import { ElMessage } from 'element-plus'

export type RecordData = Record<string, any>
export const session = reactive<{user: {id: string; username: string; role: string} | null; ready: boolean}>({user: null, ready: false})

export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = {'X-Requested-With': 'calculus-studio'}
  if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json'
  const response = await fetch('/api' + path, {method, headers, credentials: 'same-origin', body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined})
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((e: any) => e.msg).join('；') : '请求失败'
    throw new Error(detail)
  }
  return data as T
}

export async function refreshSession() {
  try {session.user = await api('/auth/me')} catch {session.user = null} finally {session.ready = true}
}

export async function action<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {return await fn()} catch (error) {ElMessage.error(error instanceof Error ? error.message : '操作失败')}
}

export async function waitTask(task: RecordData): Promise<RecordData> {
  let current = task
  while (current.state === 'pending' || current.state === 'running') {
    await new Promise(r => setTimeout(r, 1200))
    current = await api('/admin/tasks/' + task.id)
  }
  if (current.state !== 'succeeded') throw new Error(current.error || '任务失败，可在任务中心重试')
  return current
}

export const labels: Record<string, string> = {concept:'知识点', theorem:'定理', formula:'公式', example:'例题',note:'资料笔记', prerequisite:'前置知识',uses_theorem:'使用定理',uses_formula:'使用公式',assesses:'考查',confusable:'易混淆',related:'相关',chat:'知识问答',review:'复习计划',quiz:'自测练习',correct:'回答正确',incorrect:'需要复习',pending:'待核对',succeeded:'已完成',failed:'失败',running:'处理中',draft:'草稿',ready:'已导入',cancelled:'已取消',numeric:'数值题',expression:'表达式题',antiderivative:'不定积分',proof:'证明题',choice:'选择题',easy:'基础',medium:'进阶',hard:'挑战'}
