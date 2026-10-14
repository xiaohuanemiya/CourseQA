<script setup lang="ts">
import { computed } from 'vue'
import katex from 'katex'
const props = defineProps<{value?: string; block?: boolean}>()
const html = computed(() => katex.renderToString(props.value || '', {throwOnError:false, displayMode:props.block, trust:false, strict:'warn'}))
</script>
<template><span class="formula" v-html="html"></span></template>
