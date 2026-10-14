<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, action } from '../api'
import Formula from './Formula.vue'
import MathText from './MathText.vue'
const props=defineProps<{documentId:string}>()
const emit=defineEmits<{close:[];edit:[locator:string]}>()
const report=ref<any>(),busy=ref(false),filter=ref('all')
const blocks=computed(()=>report.value?.blocks.filter((b:any)=>filter.value==='all'||(filter.value==='formula'?b.label==='formula':b.warnings.length>0))||[])
watch(()=>props.documentId,async id=>{if(!id)return;busy.value=true;report.value=undefined;await action(async()=>report.value=await api(`/admin/documents/${id}/recognition`));busy.value=false},{immediate:true})
</script>
<template><el-dialog :model-value="!!documentId" title="识别结果与原图" width="95vw" @close="emit('close')">
  <div v-loading="busy" style="min-height:160px"><template v-if="report">
    <el-alert title="可对照原图查看识别结果，按需编辑公式和文字。" type="warning" :closable="false"/>
    <div class="toolbar"><span>{{report.parser}}</span><el-select v-model="filter"><el-option label="全部内容" value="all"/><el-option label="仅公式" value="formula"/><el-option label="需重点校对" value="warning"/></el-select></div>
    <article v-for="(b,i) in blocks" :key="i" class="recognition-block">
      <div><b>{{b.locator}}</b> <el-tag size="small">{{b.label}}</el-tag>
        <p v-if="b.confidence!==null" class="muted">页面文字 OCR 分数：{{Math.round(b.confidence*100)}}%</p>
        <img v-if="b.asset_id" :src="`/api/admin/documents/${documentId}/assets/${b.asset_id}`" loading="lazy" class="recognition-image" alt="对应位置的原始图像"/>
      </div><div><Formula v-if="b.latex" :value="b.latex" block/><MathText v-else :text="b.text"/>
        <pre v-if="b.latex" class="latex-source">{{b.latex}}</pre>
        <p v-for="w in b.warnings" :key="w" class="recognition-warning">{{w}}</p>
        <el-button size="small" @click="emit('edit',b.locator)">编辑对应知识</el-button>
      </div>
    </article>
  </template></div>
</el-dialog></template>
