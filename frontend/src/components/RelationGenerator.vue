<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, action } from '../api'

const props = defineProps<{kbId:string; models:any[]; defaultModel:string; knowledgeCount:number}>()
const emit = defineEmits<{changed:[]}>()
const model = ref(''), task = ref<any>(), busy = ref(false), error = ref('')
const running = computed(()=>['pending','running'].includes(task.value?.state))
const skipped = computed(()=>Object.values(task.value?.result?.skipped||{}).reduce<number>((a,b)=>a+Number(b),0))
const skipNames:Record<string,string> = {invalid_format:'格式不正确',invalid_reference:'引用无效',self_relation:'自身关系',invalid_kind:'关系类型无效',invalid_direction:'方向或端点类型无效',missing_evidence:'缺少有效原文依据',duplicate_or_withdrawn:'重复或已撤回',cycle:'前置关系循环'}
watch(()=>props.defaultModel,value=>{if(!model.value)model.value=value},{immediate:true})
let timer:ReturnType<typeof setTimeout>|undefined, stopped=false
function schedule(){if(timer)clearTimeout(timer);if(!stopped)timer=setTimeout(refresh,2500)}
async function refresh(){
  try {
    const previous=task.value
    task.value=await api(`/admin/knowledge-bases/${props.kbId}/relation-generation`)
    error.value=''
    if(task.value?.state==='succeeded'&&(previous?.id!==task.value.id||previous?.state!=='succeeded'))emit('changed')
  } catch(e){error.value=e instanceof Error?e.message:'无法读取任务状态'}
  if(running.value||error.value)schedule()
}
async function start(retry=false){
  busy.value=true
  await action(async()=>{
    task.value=await api(retry?`/admin/tasks/${task.value.id}/retry`:`/admin/knowledge-bases/${props.kbId}/relation-generation`,'POST',retry?undefined:{model_profile_id:model.value})
    if(task.value.state==='succeeded')emit('changed')
    if(running.value)schedule()
  })
  busy.value=false
}
onMounted(refresh)
onBeforeUnmount(()=>{stopped=true;if(timer)clearTimeout(timer)})
</script>

<template>
  <section class="panel relation-generator">
    <h2>模型生成知识关系</h2>
    <p class="muted">分析当前知识库的 {{knowledgeCount}} 条知识，自动生成前置、使用公式、使用定理、考查、易混淆及相关关系。跨文档查找相关内容，保留原文依据。</p>
    <div class="generate-controls">
      <label>关系生成模型 <el-select v-model="model" aria-label="关系生成模型" :disabled="running||busy" placeholder="选择生成模型"><el-option v-for="m in models" :key="m.id" :value="m.id" :label="`${m.name} · ${m.model}`"/></el-select></label>
      <el-button type="primary" :disabled="!model||knowledgeCount<2||running" :loading="busy" @click="start()">模型生成关系</el-button>
      <el-button v-if="task?.state==='failed'" :loading="busy" @click="start(true)">重试生成</el-button>
    </div>
    <p class="muted small">使用所选模型服务分批处理，可离开页面。自动跳过重复、无效引用和前置循环；保留已有关系，已撤回关系不会恢复。完成后直接保存至草稿，发布知识库后应用生效。</p>
    <el-alert v-if="knowledgeCount<2" title="至少需要两条知识，请先导入资料。" type="info" :closable="false"/>
    <el-alert v-if="error" :title="error+'，正在重新连接'" type="warning" :closable="false"/>
    <div v-if="task" class="generation-result" aria-live="polite">
      <b>{{task.state==='succeeded'?'生成完成':task.state==='failed'?'生成失败':task.state==='pending'?'等待处理':'正在生成关系'}}</b>
      <span v-if="task.result?.model_name"> · {{task.result.model_name}}</span>
      <el-progress :percentage="task.progress" :status="task.state==='failed'?'exception':task.state==='succeeded'?'success':undefined"/>
      <p v-if="task.error" class="generation-error">{{task.error}}</p>
      <template v-if="task.result">
        <p>已处理 {{task.result.completed_batches}} / {{task.result.total_batches}} 批 · 新增 {{task.result.generated_count}} 条 · 跳过 {{skipped}} 条</p>
        <div class="skip-reasons"><el-tag v-for="(count,kind) in task.result.skipped" :key="kind" type="info" size="small">{{skipNames[String(kind)]||kind}} {{count}}</el-tag></div>
        <p v-if="task.state==='succeeded'">{{task.result.message}}</p>
      </template>
      <router-link to="/admin/tasks" class="text-link">查看任务中心 →</router-link>
    </div>
  </section>
</template>

<style scoped>
.relation-generator{padding:24px;margin-bottom:20px}.relation-generator h2{margin:0 0 12px}.generate-controls{display:flex;flex-wrap:wrap;align-items:end;gap:12px}.generate-controls label{display:grid;gap:8px;flex:1;max-width:380px;min-width:200px}.generate-controls .el-select{width:100%}.generation-result{margin-top:20px;border-top:1px solid var(--border,#dce4df);padding-top:20px}.generation-result .el-progress{margin-top:12px}.generation-error{color:#bd4242}.skip-reasons{display:flex;flex-wrap:wrap;gap:6px}
</style>
