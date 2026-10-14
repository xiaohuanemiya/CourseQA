<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, action, labels } from '../api'
import Graph from '../components/Graph.vue'
import MathText from '../components/MathText.vue'
import Formula from '../components/Formula.vue'

const route=useRoute(),overview=ref<any>(),result=ref<any>(),detail=ref<any>(),busy=ref(false),tab=ref('graph')
const kb=ref(String(route.query.kb_id||'')),version=ref('draft'),query=ref(''),kind=ref(''),sources=ref(true),showLabels=ref(false)
const focus=ref(''),hops=ref(1),page=ref(1),limit=ref(150),start=ref<any>(),end=ref<any>(),path=ref<any>(),directed=ref(false)
const preview=ref('')
const typeName=(value:string)=>value==='document'?'来源文档':value==='source_document'?'来源连接':labels[value]||value
const stats=computed(()=>result.value?.stats||{})
const nodes=computed(()=>path.value?.found?path.value.nodes:result.value?.nodes||[])
const edges=computed(()=>path.value?.found?path.value.edges:result.value?.edges||[])
const selectedStorage=computed(()=>overview.value?.knowledge_bases.filter((x:any)=>!kb.value||x.id===kb.value)||[])
function parameters(){const p=new URLSearchParams({version:version.value,include_sources:String(sources.value)});if(kb.value)p.set('kb_id',kb.value);return p}
async function load(reset=false){if(reset)page.value=1;busy.value=true;path.value=undefined;await action(async()=>{const p=parameters();p.set('q',query.value);p.set('kind',kind.value);p.set('limit',String(limit.value));p.set('offset',String((page.value-1)*limit.value));p.set('hops',String(hops.value));if(focus.value)p.set('focus',focus.value);result.value=await api('/admin/graph?'+p)});busy.value=false}
async function refresh(){await action(async()=>overview.value=await api('/admin/graph/overview'));await load()}
function scopeChange(){focus.value='';detail.value=undefined;start.value=undefined;end.value=undefined;load(true)}
async function inspect(id:string){await action(async()=>detail.value=await api('/admin/graph/nodes/'+encodeURIComponent(id)+'?'+parameters()))}
function neighborhood(){focus.value=detail.value.node.id;query.value='';kind.value='';load(true)}
async function findPath(){if(!start.value||!end.value)return;await action(async()=>{const p=parameters();p.set('source',start.value.id);p.set('target',end.value.id);p.set('directed',String(directed.value));path.value=await api('/admin/graph/path?'+p);if(!path.value.found)ElMessage.info('当前范围中未找到连接路径')})}
function exportView(){const blob=new Blob([JSON.stringify({knowledge_base:kb.value||'all',version:version.value,stats:stats.value,matched:result.value?.matched,partial:result.value?.truncated,nodes:nodes.value,edges:edges.value},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='knowledge-graph-view.json';a.click();URL.revokeObjectURL(url)}
onMounted(refresh)
</script>

<template><section class="page graph-page">
  <div class="page-eyebrow">KNOWLEDGE EXPLORER / GRAPH STORE</div>
  <div class="page-title"><div><h1>图数据库</h1><p class="muted">查看真实节点、关系、来源与发布索引。点击节点展开详情和邻域。</p></div><el-button :loading="busy" @click="refresh">刷新数据</el-button></div>
  <div class="kb-stats" v-if="overview"><div><strong>{{overview.totals.nodes}}</strong><span>知识节点</span></div><div><strong>{{overview.totals.edges}}</strong><span>数学知识关系</span></div><div><strong>{{overview.totals.documents}}</strong><span>来源文档</span></div><div><strong>{{overview.totals.vectors}}</strong><span>已发布向量</span></div></div>
  <div class="panel graph-controls"><div class="graph-filters">
    <el-select v-model="kb" placeholder="全部知识库" aria-label="图谱知识库" @change="scopeChange"><el-option label="全部知识库" value=""/><el-option v-for="k in overview?.knowledge_bases||[]" :key="k.id" :label="k.name" :value="k.id"/></el-select>
    <el-select v-model="version" aria-label="图谱版本" @change="scopeChange"><el-option label="当前草稿" value="draft"/><el-option label="当前已发布版本" value="published"/></el-select>
    <el-input v-model="query" placeholder="搜索节点标题" clearable @keyup.enter="load(true)" @clear="load(true)"/>
    <el-button type="primary" @click="load(true)">搜索节点</el-button>
  </div><div class="graph-filters spaced">
    <el-select v-model="kind" placeholder="全部节点类型" aria-label="节点类型" @change="load(true)"><el-option label="全部节点类型" value=""/><el-option v-for="t in ['concept','theorem','formula','example','note','document']" :key="t" :value="t" :label="typeName(t)"/></el-select>
    <el-checkbox v-model="sources" @change="load(true)">显示来源连接</el-checkbox><el-checkbox v-model="showLabels">显示节点名称</el-checkbox>
    <el-button @click="exportView" :disabled="!nodes.length">导出当前视图</el-button>
  </div></div>
  <el-alert v-if="result&&stats.semantic_edges===0&&stats.nodes>0" class="spaced" title="当前范围尚无数学知识关系。虚线表示内容来自哪份文档；前置、使用公式等关系可通过抽取或人工维护添加。" type="info" :closable="false"/>
  <el-tabs v-model="tab" class="spaced"><el-tab-pane label="图谱探索" name="graph"/><el-tab-pane label="节点列表" name="nodes"/><el-tab-pane label="关系列表" name="edges"/><el-tab-pane label="存储与索引" name="storage"/></el-tabs>
  <template v-if="tab!=='storage'"><div class="graph-toolbar"><span class="muted">本页 {{nodes.length}} 个节点 / {{edges.length}} 条连接 · {{result?.matched||0}} 个匹配节点 · {{stats.components||0}} 个连通分量</span><div><el-select v-if="focus" v-model="hops" style="width:120px" @change="load(true)"><el-option label="一跳邻域" :value="1"/><el-option label="两跳邻域" :value="2"/></el-select><el-button v-if="focus" @click="focus='';load(true)">退出邻域</el-button><el-button v-if="path?.found" @click="path=undefined">退出路径</el-button></div></div>
    <div class="graph-workspace" v-loading="busy"><div class="panel graph-main">
      <template v-if="tab==='graph'"><Graph v-if="nodes.length" :nodes="nodes" :edges="edges" :show-labels="showLabels" @select="inspect"/><el-empty v-else description="当前范围没有可显示的节点"/><div class="graph-legend"><span><i style="background:#345e4f"></i>知识/笔记</span><span><i style="background:#d5a868"></i>公式</span><span><i style="background:#7c91ad"></i>来源文档</span><span>实线：知识关系 · 虚线：来源</span></div></template>
      <el-table v-if="tab==='nodes'" :data="nodes" @row-click="(n:any)=>inspect(n.id)" height="610" class="clickable"><el-table-column prop="title" label="节点标题" min-width="220" show-overflow-tooltip/><el-table-column label="类型" width="100"><template #default="{row}">{{typeName(row.kind)}}</template></el-table-column><el-table-column prop="degree" label="连接数" width="85"/></el-table>
      <el-table v-if="tab==='edges'" :data="edges" height="610"><el-table-column label="起点" min-width="180"><template #default="{row}"><el-button link @click="inspect(row.source_id)">{{nodes.find((n:any)=>n.id===row.source_id)?.title}}</el-button></template></el-table-column><el-table-column label="关系" width="120"><template #default="{row}">{{typeName(row.kind)}}</template></el-table-column><el-table-column label="终点" min-width="180"><template #default="{row}"><el-button link @click="inspect(row.target_id)">{{nodes.find((n:any)=>n.id===row.target_id)?.title}}</el-button></template></el-table-column><el-table-column label="依据" width="120"><template #default="{row}">{{row.derived?'文档来源映射':'知识关系'}}</template></el-table-column></el-table>
    </div><aside class="panel graph-inspector"><h3>节点详情</h3><el-empty v-if="!detail" description="点击图中的节点或列表行" :image-size="65"/><template v-else>
      <el-tag>{{typeName(detail.node.kind)}}</el-tag><h3 class="graph-node-title">{{detail.node.title}}</h3><small class="muted graph-id">{{detail.node.id}}</small>
      <div class="graph-node-actions"><el-button size="small" @click="neighborhood">探索邻域</el-button><el-button size="small" @click="start=detail.node">设为起点</el-button><el-button size="small" @click="end=detail.node">设为终点</el-button></div>
      <router-link :to="'/admin/knowledge-bases/'+detail.node.kb_id" class="text-link">打开所属知识库 ↗</router-link>
      <Formula v-if="detail.node.attributes?.latex" :value="detail.node.attributes.latex" block/>
      <MathText v-if="detail.node.content" :text="detail.node.content"/>
      <p v-if="detail.node.attributes?.conditions"><b>适用条件：</b>{{detail.node.attributes.conditions}}</p>
      <p v-if="detail.node.recognition?.parser" class="muted">识别模型：{{detail.node.recognition.parser}}</p>
      <div v-for="(s,i) in detail.node.sources||[]" :key="i" class="graph-source"><b>{{s.locator}}</b><img v-if="s.asset_id" class="recognition-image" :src="`/api/admin/documents/${s.document_id}/assets/${s.asset_id}`" alt="节点的原文位置"/><p class="muted">{{s.note}}</p><el-button v-if="s.document_id" link @click="preview=s.document_id">查看原始资料 ↗</el-button></div>
      <el-button v-if="detail.node.kind==='document'" link @click="preview=detail.node.id.replace('document:','')">查看原始资料 ↗</el-button>
      <h4>关联连接（{{detail.relation_count}}）</h4><div v-for="e in detail.relations.slice(0,20)" :key="e.id" class="graph-relation"><small>{{typeName(e.kind)}}</small><el-button link @click="inspect(e.source_id===detail.node.id?e.target_id:e.source_id)">{{e.source_id===detail.node.id?e.target_title:e.source_title}}</el-button></div>
      <details v-if="detail.node.attributes"><summary>节点属性</summary><pre class="latex-source">{{JSON.stringify(detail.node.attributes,null,2)}}</pre></details>
    </template></aside></div>
    <el-pagination class="spaced" v-model:current-page="page" :page-size="limit" :total="result?.matched||0" layout="total, prev, pager, next" @current-change="load()"/>
    <div class="panel graph-path spaced"><b>连接路径</b><span>起点：{{start?.title||'从节点详情选择'}}</span><span>终点：{{end?.title||'从节点详情选择'}}</span><el-checkbox v-model="directed">沿关系方向</el-checkbox><el-button :disabled="!start||!end" @click="findPath">查找最短路径</el-button><span v-if="path?.found">{{path.hops}} 跳</span><p class="muted small">路径采用当前“显示来源连接”选项；来源连接只代表文档出处，不表示数学推导。</p></div>
  </template>
  <section v-else class="panel"><h3>实际存储</h3><p>{{overview?.storage.description}}</p><el-descriptions border :column="2"><el-descriptions-item label="数据库">{{overview?.storage.engine}}</el-descriptions-item><el-descriptions-item label="图视图">{{overview?.storage.graph_view}}</el-descriptions-item><el-descriptions-item label="节点 / 关系表">knowledge / relations</el-descriptions-item><el-descriptions-item label="向量表">release_vectors（pgvector）</el-descriptions-item></el-descriptions>
    <article v-for="k in selectedStorage" :key="k.id" class="graph-storage-row"><h3>{{k.name}}</h3><p>{{k.nodes}} 个知识节点 · {{k.edges}} 条数学关系 · {{k.documents}} 份文档 · {{k.vectors}} 个向量</p><div class="graph-legend"><el-tag v-for="(count,t) in k.node_types" :key="t">{{typeName(String(t))}} {{count}}</el-tag></div><p class="muted" v-if="!k.releases.length">尚无发布版本，草稿不会进入学生检索。</p><el-table v-else :data="k.releases"><el-table-column prop="revision" label="发布修订"/><el-table-column prop="nodes" label="节点数"/><el-table-column prop="edges" label="关系数"/><el-table-column label="状态"><template #default="{row}">{{row.current?'当前版本':'历史版本'}}</template></el-table-column></el-table><el-table v-if="k.indexes.length" :data="k.indexes"><el-table-column prop="dimension" label="向量维度"/><el-table-column prop="count" label="索引条目"/><el-table-column prop="signature" label="模型空间签名" show-overflow-tooltip/></el-table></article>
  </section>
  <el-dialog :model-value="!!preview" title="图节点来源" width="92vw" @close="preview=''"><iframe v-if="preview" :src="'/api/admin/documents/'+preview+'/file'" class="document-preview" title="节点原始资料"></iframe></el-dialog>
</section></template>
