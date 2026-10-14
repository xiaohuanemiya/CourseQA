<script setup lang="ts">
import { computed } from 'vue'
import Formula from './Formula.vue'
const props=defineProps<{text:string}>()
const parts=computed(()=>{
  const pattern=/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$([^$\n]+?)\$/g
  const result:{text:string;math:boolean;block?:boolean}[]=[]
  let cursor=0
  for(const match of props.text.matchAll(pattern)){
    if(match.index!>cursor)result.push({text:props.text.slice(cursor,match.index),math:false})
    result.push({text:match[1]||match[2]||match[3]||match[4],math:true,block:!!(match[1]||match[2])})
    cursor=match.index!+match[0].length
  }
  if(cursor<props.text.length)result.push({text:props.text.slice(cursor),math:false})
  return result
})
</script>
<template><div class="preserve"><template v-for="(part,i) in parts" :key="i"><Formula v-if="part.math" :value="part.text" :block="part.block"/><span v-else>{{part.text}}</span></template></div></template>
