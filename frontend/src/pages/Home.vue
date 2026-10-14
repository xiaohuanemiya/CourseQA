<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, action, labels, session } from '../api'
import { Reading } from '@element-plus/icons-vue'
const apps=ref<any[]>([]), search=ref(''), loading=ref(true), filter=ref('all'), router=useRouter()
const filtered=computed(()=>apps.value.filter(a=>(!search.value||a.name.includes(search.value)||a.description.includes(search.value))&&(filter.value==='all'||a.features.includes(filter.value))))
onMounted(async()=>{await action(async()=>apps.value=await api('/apps'));loading.value=false})
function open(id:string){router.push(session.user?'/apps/'+id:{path:'/login',query:{next:'/apps/'+id}})}
</script>
<template>
  <section class="page home">
    <div class="page-eyebrow">YOUR KNOWLEDGE, CONNECTED</div>
    <section class="hero"><div><div class="hero-kicker"><span></span> 不止于答案，更接近理解</div><h1>把零散的知识，<br>连成自己的<span>思路。</span></h1><p>围绕课程资料提问，找到有出处的解答。<br>从一次提问开始，找到下一步的学习方向。</p><a href="#applications" class="hero-link">探索学习应用 <span>↘</span></a></div><div class="hero-illustration" aria-hidden="true"><div class="orbit orbit-a"></div><div class="orbit orbit-b"></div><span class="topic-label label-a">课程</span><span class="topic-label label-b">问题</span><span class="topic-label label-c">资料</span><span class="topic-label label-d">解答</span><div class="knowledge-card"><div class="small-label">FROM QUESTIONS TO UNDERSTANDING</div><div class="knowledge-flow">提问 <span>→</span> 求证 <span>→</span> 理解</div><div class="knowledge-foot"><span class="live-dot"></span> 每个知识点，都有它的联系</div></div><span class="handwritten">a little closer to clarity.</span></div></section>
    <section id="applications" class="applications-section"><div class="section-heading"><div><span class="section-number">01 /</span><h2>找到适合你的学习应用</h2></div><span class="count-label">{{apps.length}} 个已发布应用</span></div><div class="filter-bar"><div class="filter-tabs"><button v-for="f in [{id:'all',label:'全部应用'},{id:'chat',label:'知识问答'},{id:'quiz',label:'自测练习'}]" :key="f.id" :class="{selected:filter===f.id}" @click="filter=f.id">{{f.label}}</button></div><el-input v-model="search" placeholder="搜索应用…" clearable class="search-input"/></div>
      <div v-loading="loading" class="app-grid"><button v-for="(app,i) in filtered" :key="app.id" class="app-card" @click="open(app.id)"><div class="card-top"><span class="app-symbol" :class="'symbol-'+i%3"><el-icon><Reading/></el-icon></span><span class="card-index">{{String(i+1).padStart(2,'0')}} <span>↗</span></span></div><h3>{{app.name}}</h3><p>{{app.description}}</p><div class="tags"><span v-for="f in app.features" :key="f">{{labels[f]}}</span></div><div class="card-bottom"><span><span class="live-dot"></span> {{app.knowledge_bases}} 个知识库提供依据</span><span>开始学习 →</span></div></button></div>
      <el-empty v-if="!loading&&!filtered.length" description="暂时没有可用应用。管理员发布后会显示在这里。"/>
    </section>
    <div class="principles"><div><span>⌘</span><section><h4>知识彼此相连</h4><p>沿着知识关系，理解前因后果</p></section></div><div><span>↗</span><section><h4>答案有据可查</h4><p>从结论回到资料，核对每一步</p></section></div><div><span>◎</span><section><h4>练习巩固理解</h4><p>通过自测检验知识掌握情况</p></section></div></div>
  </section>
</template>
