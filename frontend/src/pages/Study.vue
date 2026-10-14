<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ChatDotRound } from '@element-plus/icons-vue'
import { api, action, labels } from '../api'
import { ChatTransportError, requestId, streamChat } from '../chatStream'
import Formula from '../components/Formula.vue'
import MathText from '../components/MathText.vue'
import AgentTrace from '../components/AgentTrace.vue'
import AnswerSupports from '../components/AnswerSupports.vue'
const id=String(useRoute().params.id), app=ref<any>(),tab=ref('chat'),busy=ref(false),question=ref(''),messages=ref<any[]>([]),conversationId=ref<string>(),conversations=ref<any[]>([])
const options=ref({suggestions:[] as string[],knowledge:[] as any[],chapters:[] as string[]}),quiz=ref<any>(),answers=ref<Record<string,string>>({}),feedback=ref<Record<string,any>>({}),count=ref(20),difficulty=ref(''),chapter=ref('')
const selectedKnowledge=ref<string[]>([])
const source=ref<any>(),sourceOpen=ref(false),showMath=ref(false),operation=ref('differentiate'),expression=ref(''),point=ref(''),assumption=ref('real')
onMounted(async()=>{await action(async()=>{app.value=await api('/apps/'+id);tab.value=app.value.features[0];options.value=await api(`/apps/${id}/study-options`);if(app.value.features.includes('chat'))conversations.value=await api(`/apps/${id}/conversations`)})})
let streamController: AbortController | undefined
let activeRequest: string | undefined
onBeforeUnmount(()=>streamController?.abort())
async function send(){
  if(busy.value||!question.value.trim())return
  const text=question.value
  const request={request_id:requestId(),message:text,conversation_id:conversationId.value,
    calculation:showMath.value?{operation:operation.value,expression:expression.value,variable:'x',point:point.value||null,assumption:assumption.value}:null}
  messages.value.push({question:text,request,result:{answer:'',citations:[],calculation:null},streaming:false,progress:'',error:'',recoverable:false})
  question.value=''
  await runMessage(messages.value[messages.value.length-1])
}
async function runMessage(message:any){
  if(busy.value)return
  const controller=new AbortController()
  streamController=controller
  activeRequest=message.request.request_id
  busy.value=true
  message.streaming=true
  message.progress='正在连接…'
  message.error=''
  message.recoverable=false
  try{
    await streamChat(`/apps/${id}/chat/stream`,message.request,({type,data})=>{
      if(type==='status')message.progress=data.message
      if(type==='delta')message.result.answer+=data.text
      if(type==='reset')message.result.answer=''
      if(type==='agent'){
        message.result.agent??={trace:[]}
        message.result.agent.trace.push(data)
        message.progress=data.message
      }
      if(type==='done'){
        message.result=data
        message.streaming=false
        conversationId.value=data.conversation_id
      }
    },controller.signal)
    await action(async()=>{conversations.value=await api(`/apps/${id}/conversations`)})
  }catch(error){
    message.result={answer:'',citations:[],calculation:null,agent:message.result.agent}
    message.recoverable=error instanceof ChatTransportError&&!controller.signal.aborted
    message.error=controller.signal.aborted?'已停止生成，可重新发送问题。':(error instanceof Error?error.message:'回答未完成，请重试')
    if(!question.value&&!message.recoverable)question.value=message.question
  }finally{
    message.streaming=false
    if(streamController===controller){streamController=undefined;activeRequest=undefined}
    busy.value=false
  }
}
function stopGeneration(){
  const key=activeRequest
  streamController?.abort()
  if(key)void fetch(`/api/apps/${id}/chat/requests/${encodeURIComponent(key)}/cancel`,{
    method:'POST',credentials:'same-origin',headers:{'X-Requested-With':'calculus-studio'},
  }).catch(()=>{})
}
function supportSource(message:any,number:string){
  const citation=message.result.citations.find((item:any)=>String(item.number)===number)
  if(citation)void openSource(citation.knowledge_id,citation.release_id)
}

async function openSource(key:string,releaseId?:string){await action(async()=>{source.value=await api(`/apps/${id}/sources/${key}`+(releaseId?"?release_id="+releaseId:""));sourceOpen.value=true})}
async function createQuiz(){busy.value=true;await action(async()=>{quiz.value=await api(`/apps/${id}/quizzes`,'POST',{count:count.value,difficulty:difficulty.value||null,chapter:chapter.value||null,knowledge_ids:selectedKnowledge.value});answers.value={};feedback.value={}});busy.value=false}
async function submit(key:string){busy.value=true;await action(async()=>{feedback.value[key]=await api(`/apps/${id}/quizzes/${quiz.value.id}/answers`,'POST',{question_id:key,answer:answers.value[key]||''})});busy.value=false}
async function loadConversation(key:string){if(busy.value)return;await action(async()=>{messages.value=await api(`/apps/${id}/conversations/${key}`);conversationId.value=key})}
</script>
<template><section class="page study-page" v-if="app"><div class="page-eyebrow">LEARNING SPACE / {{app.name}}</div><div class="page-title"><div><h1>{{app.name}}</h1><p class="muted">{{app.description}}</p></div><el-tag type="success" effect="plain">{{app.knowledge_bases}} 个知识库 · 已发布</el-tag></div>
  <el-tabs v-model="tab"><el-tab-pane v-for="f in app.features" :key="f" :label="labels[f]" :name="f"/></el-tabs>
  <div v-if="tab==='chat'" class="chat-layout"><section class="chat-main panel"><div v-if="!messages.length" class="chat-welcome"><span class="welcome-icon"><el-icon><ChatDotRound/></el-icon></span><h2>从一个问题开始。</h2><p>问一个知识点，或比较两个概念。<br>回答中的来源可以随时展开核对。</p><div class="prompt-chips"><button v-for="title in options.suggestions" :key="title" @click="question='请解释'+title">{{title}} ↗</button></div></div><div class="message-list"><article v-for="(message,i) in messages" :key="i" class="message"><div class="question-bubble">{{message.question}}</div><div class="answer-head"><span class="small-avatar"><el-icon><ChatDotRound/></el-icon></span><b>课程助教</b><el-tag v-if="message.result.mode==='demo'" size="small" type="warning">演示证据模式</el-tag></div><p v-if="message.streaming" class="stream-status" role="status">{{message.progress}}</p><el-alert v-if="message.error" :title="message.error" type="warning" :closable="false" show-icon/><el-button v-if="message.recoverable" :disabled="busy" @click="runMessage(message)">检查这次回答</el-button><AgentTrace :execution="message.result.agent"/><MathText v-if="message.result.answer" class="answer-text" :aria-busy="message.streaming" :text="message.result.answer"/><AnswerSupports :supports="message.result.supports" @source="supportSource(message,$event)"/><div class="citations"><button v-for="citation in message.result.citations" :key="citation.number" @click="openSource(citation.knowledge_id,citation.release_id)"><span>{{citation.number}}</span>{{citation.title}} ↗</button></div><div v-if="message.result.calculation" class="math-result"><b>数学工具结果</b><Formula v-if="message.result.calculation.latex" :value="message.result.calculation.latex" block/><p v-else>{{message.result.calculation.message||message.result.calculation.reason}}</p><small>假设：{{message.result.calculation.assumption}} · 工具结果独立于模型解释</small></div></article></div><div class="composer"><div v-if="showMath" class="math-controls"><el-select v-model="operation"><el-option label="求导" value="differentiate"/><el-option label="积分" value="integrate"/><el-option label="极限" value="limit"/></el-select><el-input v-model="expression" placeholder="表达式，如 sin(x)/x"/><el-input v-if="operation==='limit'" v-model="point" placeholder="趋近点，如 0"/><el-select v-model="assumption"><el-option label="x 为实数" value="real"/><el-option label="x > 0" value="positive"/></el-select></div><el-input v-model="question" type="textarea" :rows="3" placeholder="描述你的问题，尽量包含题目条件…" @keydown.ctrl.enter.prevent="send"/><div class="composer-bottom"><el-checkbox v-model="showMath">附带数学计算</el-checkbox><el-button v-if="busy" @click="stopGeneration">停止生成</el-button><el-button v-else type="primary" :disabled="!question.trim()" @click="send">发送问题 ↑</el-button></div></div></section><aside class="study-aside"><div class="panel"><h3>学习对话</h3><el-button plain class="full-width" :disabled="busy" @click="messages=[];conversationId=undefined">＋ 新建对话</el-button><button v-for="c in conversations" :key="c.id" class="conversation-link" :disabled="busy" @click="loadConversation(c.id)">{{c.title}}</button></div><div class="quiet-note"><span>↗</span><h4>先看依据，再理解答案。</h4><p>概念与结论都有适用背景。查看课程原文，是理解的一部分。</p></div></aside></div>
  <section v-if="tab==='quiz'"><div class="panel quiz-config"><div><h3>给自己一次小测</h3><p class="muted">题库不足时按实际数量出题。每道题只计入一次作答。</p></div><div class="inline-form"><el-select v-model="chapter" clearable placeholder="所有章节"><el-option v-for="c in options.chapters" :key="c" :value="c" :label="c"/></el-select><el-select v-model="difficulty" clearable placeholder="所有难度"><el-option v-for="d in ['easy','medium','hard']" :key="d" :value="d" :label="labels[d]"/></el-select><el-select v-model="selectedKnowledge" multiple collapse-tags clearable placeholder="所有知识点"><el-option v-for="k in options.knowledge" :key="k.id" :value="k.id" :label="k.title"/></el-select><el-input-number v-model="count" :min="1" :max="50"/><el-button type="primary" :loading="busy" @click="createQuiz">生成自测</el-button></div></div><div v-if="quiz" class="quiz-list"><div class="muted">本次共 {{quiz.count}} 道题 · 表达式用 * 表示乘法，^ 表示乘方，不定积分保留 C</div><el-empty v-if="!quiz.count" description="没有符合条件的已发布题目，请更换筛选条件"/><article v-for="(q,i) in quiz.questions" :key="q.id" class="panel question-card"><div class="question-heading"><span class="section-number">{{String(Number(i)+1).padStart(2,'0')}}</span><el-tag size="small">{{labels[q.kind]}}</el-tag><el-tag size="small" type="info">{{labels[q.attributes.difficulty]||'进阶'}}</el-tag></div><h3>{{q.title}}</h3><p v-if="q.attributes.options">{{q.attributes.options}}</p><div class="inline-form"><el-input v-model="answers[q.id]" placeholder="输入你的答案" :disabled="!!feedback[q.id]"/><el-button type="primary" :loading="busy" :disabled="!!feedback[q.id]||!answers[q.id]" @click="submit(q.id)">提交答案</el-button></div><div v-if="feedback[q.id]" class="feedback" :class="feedback[q.id].verdict"><b>{{labels[feedback[q.id].verdict]}}</b><p>{{feedback[q.id].reason}}</p><p>参考答案：{{feedback[q.id].answer}}</p><p>{{feedback[q.id].explanation}}</p><button v-for="k in q.knowledge_ids" :key="k" class="text-link" @click="openSource(k)">回看相关知识 ↗</button></div></article></div></section>
  <el-drawer v-model="sourceOpen" title="知识与来源" size="min(620px, 95vw)"><template v-if="source"><el-tag>{{labels[source.kind]}}</el-tag><h2>{{source.title}}</h2><Formula v-if="source.attributes.latex" :value="source.attributes.latex" block/><p class="preserve">{{source.content}}</p><div v-if="source.attributes.conditions" class="quiet-note">适用条件：{{source.attributes.conditions}}</div><h3 class="spaced">来源依据</h3><div v-for="(s,i) in source.sources" :key="i" class="source-card"><b>{{s.locator}}</b><img v-if="s.asset_id" :src="`/api/apps/${id}/documents/${s.document_id}/assets/${s.asset_id}`" class="recognition-image" alt="来源位置原图"/><p class="preserve">{{s.excerpt}}</p><p class="muted">{{s.note}}</p><a v-if="s.document_id" :href="`/api/apps/${id}/documents/${s.document_id}`" target="_blank" rel="noopener">打开原始文件 ↗</a></div></template></el-drawer>
</section></template>
