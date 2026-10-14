<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { api, action, labels } from '../api'
const tasks=ref<any[]>([])
async function load(){tasks.value=await api('/admin/tasks')}
let timer:ReturnType<typeof setInterval>
onMounted(()=>{action(load);timer=setInterval(()=>action(load),4000)})
onBeforeUnmount(()=>clearInterval(timer))
</script>
<template><section class="page"><div class="page-eyebrow">ADMINISTRATION / TASKS</div><div class="page-title"><div><h1>任务中心</h1><p class="muted">查看资料导入、知识关系生成与版本发布的处理结果。</p></div><el-button @click="action(load)">刷新</el-button></div><el-table :data="tasks" class="panel"><el-table-column label="任务" min-width="150"><template #default="{row}">{{{ingest:'资料导入',publish:'知识库发布',publish_app:'应用发布',generate_relations:'知识关系生成'}[row.kind as string]||row.kind}}<div class="muted small">{{row.id.slice(0,8)}}</div></template></el-table-column><el-table-column label="状态" width="120"><template #default="{row}"><el-tag :type="row.state==='failed'?'danger':row.state==='succeeded'?'success':'info'">{{labels[row.state]||row.state}}</el-tag></template></el-table-column><el-table-column label="进度" width="180"><template #default="{row}"><el-progress :percentage="row.progress"/></template></el-table-column><el-table-column label="结果" min-width="220"><template #default="{row}">{{row.error|| (row.result?JSON.stringify(row.result):'等待处理')}}</template></el-table-column><el-table-column label="操作" width="100"><template #default="{row}"><el-button v-if="row.state==='failed'" size="small" @click="action(async()=>{await api('/admin/tasks/'+row.id+'/retry','POST');await load()})">重试</el-button></template></el-table-column></el-table></section></template>
