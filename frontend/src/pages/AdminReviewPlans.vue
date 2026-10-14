<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, action, session } from '../api'
import MathText from '../components/MathText.vue'
import ProgressChart from '../components/ProgressChart.vue'

const route=useRoute(), apps=ref<any[]>([]), learners=ref<any[]>([]), appId=ref(''), learnerId=ref('')
const plan=ref<any>(), busy=ref(false), page=ref(1), search=ref(''), source=ref<any>(), sourceOpen=ref(false)
const filtered=computed(()=>plan.value?.items.filter((item:any)=>item.title.includes(search.value))||[])
const visible=computed(()=>filtered.value.slice((page.value-1)*15,page.value*15))
const assessed=computed(()=>plan.value?.items.filter((item:any)=>item.mastery!==null)||[])

async function loadPlan(){
  plan.value=undefined;page.value=1;search.value=''
  if(!appId.value||!learnerId.value)return
  busy.value=true
  await action(async()=>{plan.value=await api(`/admin/apps/${appId.value}/review-plan?user_id=${encodeURIComponent(learnerId.value)}`)})
  busy.value=false
}
async function loadLearners(){
  plan.value=undefined;learners.value=[];learnerId.value=''
  if(!appId.value)return
  busy.value=true
  await action(async()=>{
    learners.value=await api(`/admin/apps/${appId.value}/learners`)
    learnerId.value=learners.value.find(user=>user.id===session.user?.id)?.id||learners.value[0]?.id||''
    await loadPlan()
  })
  busy.value=false
}
onMounted(async()=>{
  busy.value=true
  await action(async()=>{
    apps.value=(await api('/admin/apps')).filter((app:any)=>app.active&&app.published)
    appId.value=apps.value.find(app=>app.id===route.query.app_id)?.id||apps.value[0]?.id||''
    await loadLearners()
  })
  busy.value=false
})
async function openSource(id:string){
  await action(async()=>{source.value=await api(`/apps/${appId.value}/sources/${id}`);sourceOpen.value=true})
}
</script>

<template>
  <section class="page admin-review-page">
    <div class="page-eyebrow">ADMINISTRATION / REVIEW PLANS</div>
    <div class="page-title"><div><h1>复习计划</h1><p class="muted">按应用与学习用户查看掌握程度、复习建议和错题记录。</p></div></div>
    <div class="panel review-filters">
      <el-select v-model="appId" aria-label="学习应用" placeholder="选择已发布应用" :disabled="busy" @change="loadLearners">
        <el-option v-for="app in apps" :key="app.id" :value="app.id" :label="app.published.name"/>
      </el-select>
      <el-select v-model="learnerId" aria-label="学习用户" placeholder="选择学习用户" :disabled="busy||!appId" @change="loadPlan">
        <el-option v-for="user in learners" :key="user.id" :value="user.id" :label="user.username+(user.id===session.user?.id?'（当前账号）':'')"/>
      </el-select>
      <el-button :loading="busy" :disabled="!learnerId" @click="loadPlan">刷新计划</el-button>
    </div>
    <el-empty v-if="!busy&&!apps.length" description="发布学习应用后，可在这里查看复习计划"/>
    <section v-if="plan" v-loading="busy">
      <div class="review-banner"><div><h2>{{plan.learner.username}} 的复习计划</h2><p>依据此应用中的作答记录与前置知识生成；没有记录时标记为尚未评估。</p></div><div class="review-stat">{{plan.total_attempts}}<small>累计作答</small></div></div>
      <div v-if="assessed.length" class="panel"><h3>掌握程度概览</h3><p class="muted">按每个知识点最近 20 次有效作答估计。</p><ProgressChart :items="assessed"/></div>
      <div class="panel spaced">
        <div class="toolbar"><h3>复习建议</h3><el-input v-model="search" placeholder="搜索知识点" clearable @input="page=1"/></div>
        <el-table :data="visible" empty-text="没有符合条件的知识点">
          <el-table-column prop="title" label="知识点" min-width="190"/>
          <el-table-column label="掌握程度" width="135"><template #default="{row}">{{row.mastery===null?'尚未评估':Math.round(row.mastery*100)+'%'}}</template></el-table-column>
          <el-table-column label="推荐依据" min-width="260"><template #default="{row}"><p>{{row.reason}}</p><small class="muted">资料覆盖 {{row.importance.document_frequency||0}} · PageRank {{Number(row.importance.pagerank||0).toFixed(3)}} · 往年题 {{row.importance.past_exam_frequency||0}}</small></template></el-table-column>
          <el-table-column label="来源" width="105"><template #default="{row}"><el-button text @click="openSource(row.id)">查看知识</el-button></template></el-table-column>
        </el-table>
        <el-pagination v-model:current-page="page" :page-size="15" :total="filtered.length" layout="prev, pager, next" :pager-count="5" class="spaced"/>
      </div>
      <h2 class="spaced">错题记录</h2>
      <el-empty v-if="!plan.mistakes.length" description="此用户在当前应用中还没有错题记录"/>
      <article v-for="(item,index) in plan.mistakes" :key="index" class="panel mistake"><h3>{{item.title}}</h3><p>作答：{{item.answer}}</p><p>参考答案：{{item.feedback.answer}}</p><p class="muted">{{item.feedback.explanation}}</p></article>
    </section>
    <el-drawer v-model="sourceOpen" title="知识与来源" size="min(620px, 95vw)">
      <template v-if="source"><h2>{{source.title}}</h2><MathText :text="source.content"/><h3 class="spaced">来源依据</h3><div v-for="(item,index) in source.sources" :key="index" class="source-card"><b>{{item.locator}}</b><img v-if="item.asset_id" :src="`/api/apps/${appId}/documents/${item.document_id}/assets/${item.asset_id}`" class="recognition-image" alt="来源位置原图"/><p class="preserve">{{item.excerpt}}</p><a v-if="item.document_id" :href="`/api/apps/${appId}/documents/${item.document_id}`" target="_blank" rel="noopener">打开原始文件 ↗</a></div></template>
    </el-drawer>
  </section>
</template>

<style scoped>
.review-filters{display:flex;gap:16px;flex-wrap:wrap}.review-filters .el-select{width:260px;max-width:100%}.review-banner h2{overflow-wrap:anywhere}.toolbar h3{margin:0 auto 0 0}
@media(max-width:600px){.review-filters .el-select{width:100%}.review-banner{padding:20px;gap:12px;align-items:flex-start}.review-banner h2{font-size:19px}}
</style>
