<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, session } from '../api'

type State = 'queued' | 'uploading' | 'pending' | 'running' | 'succeeded' | 'upload_failed' | 'failed' | 'invalid' | 'skipped'
type Options = {mode:string; page_start:number; page_end:number|null; splitting:Record<string, unknown>}
type Task = {id:string; state:string; progress:number; error?:string}
type Row = {
  key:string; name:string; size:number; file?:File; options:Options; settings:string;
  state:State; progress:number; error:string; statusError?:string; taskId?:string; documentId?:string; retrying?:boolean;
}
const props = defineProps<{kbId:string; options:Options; settingsLabel:string}>()
const emit = defineEmits<{changed:[]}>()
const input = ref<HTMLInputElement>(), rows = ref<Row[]>([])
const supported = /\.(pdf|pptx|docx|md|txt|png|jpg|jpeg|webp|bmp|tif|tiff)$/i
const active = new Set<State>(['queued','uploading','pending','running'])
const storageKey = `document-uploads:${session.user?.id}:${props.kbId}`
const states:Record<State,string> = {queued:'等待上传',uploading:'上传中',pending:'等待处理',running:'解析与切分中',
  succeeded:'已导入',upload_failed:'上传失败',failed:'处理失败',invalid:'文件不可用',skipped:'文件已存在'}
const counts = computed(()=>({
  total:rows.value.length, completed:rows.value.filter(r=>r.state==='succeeded').length,
  active:rows.value.filter(r=>active.has(r.state)).length,
  failed:rows.value.filter(r=>['upload_failed','failed','invalid'].includes(r.state)).length,
  skipped:rows.value.filter(r=>r.state==='skipped').length,
}))
const hasUnsent = computed(()=>rows.value.some(r=>r.state==='queued'||r.state==='uploading'))
let disposed = false, sending = false, polling = false
let timer:ReturnType<typeof setTimeout>|undefined

function persist(){
  // Files stay in memory; only task references and non-secret import settings survive a reload.
  try {sessionStorage.setItem(storageKey,JSON.stringify(rows.value.map(({file,...row})=>row)))} catch { /* Storage may be disabled. */ }
}
function selectFiles(){input.value?.click()}
defineExpose({selectFiles})
function formatSize(size:number){return size>=1024*1024?`${(size/1024/1024).toFixed(1)} MB`:`${Math.ceil(size/1024)} KB`}
function errorMessage(error:unknown){return error instanceof Error?error.message:'请求失败，请重试'}
function detail(data:any,status:number){
  return typeof data?.detail==='string'?data.detail:Array.isArray(data?.detail)?data.detail.map((d:any)=>d.msg).join('；'):
    status===413?'文件超过上传大小限制':`上传失败（HTTP ${status}）`
}

function addFiles(files:File[]){
  if(props.options.splitting.method==='semantic'&&!props.options.splitting.model_profile_id){
    ElMessage.warning('请先选择语义切分模型，再选择文件');return
  }
  const options=JSON.parse(JSON.stringify(props.options)) as Options
  if(options.page_end&&options.page_end<options.page_start){ElMessage.warning('结束页不能小于起始页');return}
  let added=0
  for(const file of files){
    const key=`${file.name}:${file.size}:${file.lastModified}`
    const old=rows.value.find(r=>r.key===key)
    if(old){
      if(old.state==='upload_failed'){
        old.file=file;old.state='queued';old.error='';old.progress=0;added++
      }
      continue
    }
    const error=!supported.test(file.name)?'不支持此文件格式':!file.size?'文件为空':file.size>30*1024*1024?'单个文件不能超过 30 MB':''
    rows.value.push({key,name:file.name,size:file.size,file:error?undefined:file,options,settings:props.settingsLabel,
                    state:error?'invalid':'queued',error,progress:0})
    added++
  }
  if(!added&&files.length)ElMessage.info('所选文件已在当前队列中')
  persist();void sendNext()
}
function picked(){
  const files=Array.from(input.value?.files||[])
  if(input.value)input.value.value=''
  addFiles(files)
}
function dropped(event:DragEvent){addFiles(Array.from(event.dataTransfer?.files||[]))}

function upload(row:Row):Promise<{document:{id:string};task:Task}>{
  return new Promise((resolve,reject)=>{
    const body=new FormData()
    body.append('file',row.file!)
    body.append('mode',row.options.mode)
    body.append('page_start',String(row.options.page_start))
    if(row.options.page_end)body.append('page_end',String(row.options.page_end))
    body.append('splitting',JSON.stringify(row.options.splitting))
    const xhr=new XMLHttpRequest()
    xhr.open('POST',`/api/admin/knowledge-bases/${encodeURIComponent(props.kbId)}/documents`)
    xhr.withCredentials=true
    xhr.setRequestHeader('X-Requested-With','calculus-studio')
    xhr.upload.onprogress=event=>{if(event.lengthComputable)row.progress=Math.round(event.loaded/event.total*100)}
    xhr.onload=()=>{
      let data:any
      try {data=JSON.parse(xhr.responseText)} catch {data=null}
      if(xhr.status>=200&&xhr.status<300&&data?.document?.id&&data?.task?.id){resolve(data);return}
      const error=new Error(detail(data,xhr.status)) as Error&{status:number}
      error.status=xhr.status;reject(error)
    }
    xhr.onerror=()=>reject(new Error('网络连接中断，请重试；已接收的相同文件不会重复导入'))
    xhr.onabort=()=>reject(new Error('上传已中断，请重新选择文件'))
    xhr.send(body)
  })
}
function applyTask(row:Row,task:Task){
  row.taskId=task.id
  row.progress=Math.max(0,Math.min(100,task.progress||0))
  row.state=task.state==='succeeded'?'succeeded':task.state==='failed'||task.state==='cancelled'?'failed':
    task.state==='running'?'running':'pending'
  row.error=row.state==='failed'?(task.error||'处理失败，可单独重试'):''
  row.statusError=''
  if(row.state==='succeeded')row.progress=100
}
async function sendNext(){
  if(sending||disposed)return
  sending=true
  try {
    while(!disposed){
      const row=rows.value.find(r=>r.state==='queued'&&r.file)
      if(!row)break
      row.state='uploading';row.progress=0;persist()
      try {
        const result=await upload(row)
        row.documentId=result.document.id;row.file=undefined
        applyTask(row,result.task)
        if(!disposed)emit('changed')
      } catch(error){
        const duplicate=(error as {status?:number}).status===409
        row.state=duplicate?'skipped':'upload_failed';row.error=errorMessage(error)
        if(duplicate)row.file=undefined
      }
      persist();schedulePoll()
    }
  } finally {sending=false}
}
function schedulePoll(delay=1500){
  if(disposed||polling||timer||!rows.value.some(r=>r.taskId&&['pending','running'].includes(r.state)))return
  timer=setTimeout(()=>{timer=undefined;void poll()},delay)
}
async function poll(){
  if(disposed||polling)return
  polling=true
  let changed=false,failed=false
  try {
    const pending=rows.value.filter(r=>r.taskId&&!r.retrying&&['pending','running'].includes(r.state))
    // Limit status traffic even when hundreds of files are queued.
    for(let i=0;i<pending.length&&!disposed;i+=4){
      await Promise.all(pending.slice(i,i+4).map(async row=>{
        try {
          const taskId=row.taskId
          const task=await api<Task>('/admin/tasks/'+taskId)
          if(row.taskId!==taskId||row.retrying)return
          const previous=row.state;applyTask(row,task)
          if(row.state!==previous&&['succeeded','failed'].includes(row.state))changed=true
        } catch(error){failed=true;row.statusError='暂时无法刷新状态：'+errorMessage(error)}
      }))
    }
  } finally {
    polling=false;persist()
    if(changed&&!disposed)emit('changed')
    schedulePoll(failed?6000:1500)
  }
}
async function retry(row:Row){
  if(row.state==='upload_failed'&&row.file){row.state='queued';row.error='';persist();void sendNext();return}
  if(row.state!=='failed'||!row.taskId||row.retrying)return
  row.state='pending';row.retrying=true;row.error='';row.progress=0;persist()
  try {applyTask(row,await api<Task>('/admin/tasks/'+row.taskId+'/retry','POST'))}
  catch(error){row.state='failed';row.error=errorMessage(error)}
  finally {row.retrying=false}
  persist();schedulePoll()
  if(!disposed)emit('changed')
}
function remove(row:Row){
  if(active.has(row.state))return
  rows.value=rows.value.filter(r=>r.key!==row.key);persist()
}
function clearFinished(){rows.value=rows.value.filter(r=>!['succeeded','skipped'].includes(r.state));persist()}
function beforeUnload(event:BeforeUnloadEvent){if(hasUnsent.value){event.preventDefault();event.returnValue=''}}
onMounted(()=>{
  try {
    const saved=JSON.parse(sessionStorage.getItem(storageKey)||'[]')
    if(Array.isArray(saved))rows.value=saved.filter(r=>r.key&&r.name&&r.options&&r.state in states).map(row=>({
      ...row,file:undefined,retrying:false,...(['queued','uploading'].includes(row.state)?{
        state:'upload_failed',progress:0,error:'此文件尚未确认上传，请重新选择；已上传文件可在下方资料列表查看'}:{})}))
  } catch { /* Ignore stale browser state. */ }
  window.addEventListener('beforeunload',beforeUnload);schedulePoll(0)
})
onBeforeUnmount(()=>{
  disposed=true
  if(timer)clearTimeout(timer)
  window.removeEventListener('beforeunload',beforeUnload)
  // Stop unsent files; accepted server tasks continue and can be restored on return.
  persist()
})
</script>

<template>
  <div class="batch-upload" @dragover.prevent @drop.prevent="dropped">
    <input ref="input" type="file" multiple hidden accept=".pdf,.pptx,.docx,.md,.txt,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff" @change="picked"/>
    <button type="button" class="upload-dropzone" @click="selectFiles">选择多份资料，或拖放文件到这里 <span>PDF、图片、Office、Markdown、TXT · 每份最大 30 MB</span></button>
    <template v-if="rows.length">
      <div class="batch-upload-summary" role="status">
        <span>共 {{counts.total}} 份 · 已导入 {{counts.completed}} · 进行中 {{counts.active}} · 失败 {{counts.failed}}<template v-if="counts.skipped"> · 已存在 {{counts.skipped}}</template></span>
        <div><el-button v-if="counts.completed||counts.skipped" text @click="clearFinished">清除已完成记录</el-button><router-link to="/admin/tasks" class="text-link">任务中心 ↗</router-link></div>
      </div>
      <p class="form-help">每次选择的文件使用当时的切分设置。等待所有文件上传后再离开；已上传资料会在后台继续处理。</p>
      <ul class="upload-queue" aria-label="批量上传队列">
        <li v-for="row in rows" :key="row.key" class="upload-row" :data-state="row.state">
          <div class="upload-file-info"><strong>{{row.name}}</strong><small>{{formatSize(row.size)}} · {{row.settings}}</small>
            <p v-if="row.error" :class="row.state==='skipped'?'muted':'upload-error'">{{row.error}}</p>
            <p v-if="row.statusError" class="upload-error">{{row.statusError}}</p>
            <small v-if="row.state==='upload_failed'&&!row.file">重新选择此文件后可继续上传。</small>
          </div>
          <div class="upload-file-progress"><span>{{states[row.state]}}</span><el-progress :percentage="row.progress" :status="row.state==='succeeded'?'success':undefined"/></div>
          <div class="upload-file-actions">
            <el-button v-if="row.state==='failed'||row.state==='upload_failed'&&row.file" size="small" @click="retry(row)">{{row.taskId?'重试处理':'重试上传'}}</el-button>
            <el-button v-if="row.statusError" size="small" @click="poll">刷新状态</el-button>
            <el-button v-if="!active.has(row.state)" text size="small" @click="remove(row)">移除记录</el-button>
          </div>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.batch-upload{margin:20px 0 28px;min-width:0}.upload-dropzone{width:100%;padding:24px;border:1px dashed #a9bca8;border-radius:10px;background:#f5f8f1;color:#48674d;font-size:14px;line-height:1.8}.upload-dropzone span{display:block;margin-top:7px;font-size:11px;color:#819078}.batch-upload-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:20px;font-size:12px}.batch-upload-summary>div{display:flex;align-items:center;gap:12px}.upload-queue{list-style:none;padding:0;margin:12px 0;border:1px solid #e1e7da;border-radius:10px;background:white;overflow:hidden}.upload-row{display:grid;grid-template-columns:minmax(0,1fr) 175px 165px;gap:18px;align-items:center;padding:18px;border-bottom:1px solid #edf0e7}.upload-row:last-child{border-bottom:0}.upload-file-info{min-width:0}.upload-file-info strong{display:block;overflow-wrap:anywhere;font-size:13px}.upload-file-info small{display:block;font-size:11px;color:#87947d;margin-top:6px;overflow-wrap:anywhere}.upload-file-info p{margin:7px 0 0;font-size:12px;overflow-wrap:anywhere}.upload-error{color:#b45e4b}.upload-file-progress>span{display:block;font-size:11px;color:#6e8264;margin-bottom:10px}.upload-file-actions{display:flex;flex-wrap:wrap;gap:5px;justify-content:flex-end}.upload-file-actions .el-button+.el-button{margin-left:0}@media(max-width:1000px){.upload-row{grid-template-columns:minmax(0,1fr) 150px}.upload-file-actions{grid-column:1/-1}}@media(max-width:600px){.upload-row{grid-template-columns:minmax(0,1fr);gap:12px}.upload-file-actions{justify-content:flex-start}.upload-dropzone{padding:18px 12px}.batch-upload-summary{line-height:1.8}}
</style>
