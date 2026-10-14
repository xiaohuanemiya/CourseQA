<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { labels } from '../api'
echarts.use([GraphChart, TooltipComponent, CanvasRenderer])
const props = withDefaults(defineProps<{nodes: any[]; edges: any[]; showLabels?:boolean}>(),{showLabels:true})
const emit = defineEmits<{select: [id: string]}>()
const container = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
function draw() {
  chart?.setOption({tooltip:{renderMode:'richText'}, series:[{type:'graph', layout:'force', roam:true,zoom:props.nodes.length>80?.55:props.nodes.length>30?.75:1,
    label:{show:props.showLabels,position:'bottom',color:'#27473d',fontSize:12,formatter:(p:any)=>p.name.length>20?p.name.slice(0,20)+'…':p.name},
    force:{repulsion:350,edgeLength:130},edgeSymbol:['none','arrow'],symbolSize:24,
    data:props.nodes.map(n => ({id:n.id,name:n.title,symbol:n.kind==='document'?'roundRect':'circle',symbolSize:n.kind==='document'?36:Math.min(40,17+Math.sqrt(n.degree||1)*3),itemStyle:{color:n.kind==='document'?'#7c91ad':n.kind==='formula'?'#d5a868':n.kind==='theorem'?'#709e93':'#345e4f'}})),
    links:props.edges.map(e=>({id:e.id,source:e.source_id,target:e.target_id,name:labels[e.kind]||e.kind,lineStyle:{type:e.derived?'dashed':'solid',color:e.derived?'#c0cbd6':'#658875',curveness:.08}})),
    emphasis:{focus:'adjacency'},lineStyle:{width:1.5}}]})
}
onMounted(()=>{chart=echarts.init(container.value!);draw();chart.on('click',p=>{if(p.dataType==='node')emit('select',String((p.data as any).id))});observer=new ResizeObserver(()=>chart?.resize());observer.observe(container.value!)})
watch(()=>[props.nodes,props.edges,props.showLabels],draw,{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="container" class="graph-canvas" role="img" aria-label="知识关系图，点击节点查看内容"></div></template>
