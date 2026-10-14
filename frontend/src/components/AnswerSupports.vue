<script setup lang="ts">
import MathText from './MathText.vue'
defineProps<{supports?: {claim: string; citation_id: string; quote: string}[]}>()
defineEmits<{source: [citationId: string]}>()
</script>

<template>
  <details v-if="supports?.length" class="answer-supports">
    <summary>查看结论对应的原文</summary>
    <p class="hint">可对照以下摘录核对结论及其适用条件。</p>
    <section v-for="(support,index) in supports" :key="index">
      <MathText :text="support.claim" />
      <blockquote><MathText :text="support.quote" /></blockquote>
      <button class="text-link" @click="$emit('source',support.citation_id)">[{{ support.citation_id }}] 查看完整来源 ↗</button>
    </section>
  </details>
</template>

<style scoped>
.answer-supports { margin: 16px 0; padding: 12px 16px; border: 1px solid var(--el-border-color); border-radius: 8px; overflow-wrap: anywhere; }
summary { cursor: pointer; }
.hint { color: var(--el-text-color-secondary); font-size: 13px; }
section + section { margin-top: 18px; }
blockquote { margin: 8px 0; padding: 8px 12px; border-left: 3px solid var(--el-border-color); background: var(--el-fill-color-light); }
</style>
