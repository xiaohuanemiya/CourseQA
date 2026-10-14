<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])
const props=defineProps<{items:any[]}>()
const container=ref<HTMLDivElement>()
let chart:echarts.ECharts|undefined, observer:ResizeObserver|undefined
function draw(){
  const assessed=props.items.filter(i=>i.mastery!==null).slice(0,10)
  chart?.setOption({grid:{left:12,right:35,top:12,bottom:10,containLabel:true},
    tooltip:{trigger:'axis',valueFormatter:(value:any)=>`${value}%`},
    xAxis:{type:'value',min:0,max:100,axisLabel:{formatter:'{value}%'}},
    yAxis:{type:'category',data:assessed.map(i=>i.title),axisLabel:{width:110,overflow:'truncate'}},
    series:[{type:'bar',data:assessed.map(i=>Math.round(i.mastery*100)),barMaxWidth:26,
      itemStyle:{color:'#426953',borderRadius:[0,5,5,0]}}]})
}
onMounted(()=>{chart=echarts.init(container.value!);draw();observer=new ResizeObserver(()=>chart?.resize());observer.observe(container.value!)})
watch(()=>props.items,draw,{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div ref="container" style="height:280px;width:100%" role="img" aria-label="已评估知识点的掌握程度估计"></div></template>
