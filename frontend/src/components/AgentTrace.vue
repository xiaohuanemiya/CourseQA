<script setup lang="ts">
defineProps<{ execution?: any }>()
const seconds = (ms: number) => (ms / 1000).toFixed(1) + ' 秒'
</script>

<template>
  <details v-if="execution?.trace?.length" class="agent-trace">
    <summary>查看学习过程<span v-if="execution.subagents"> · {{ execution.subagents }} 个子任务</span><span v-if="execution.elapsed_ms != null"> · {{ seconds(execution.elapsed_ms) }}</span></summary>
    <p v-if="execution.model_calls != null" class="metrics">
      模型调用 {{ execution.model_calls }} 次 · 工具 {{ execution.tool_calls }} 次
      <span v-if="execution.cache_hits">（复用 {{ execution.cache_hits }} 次）</span>
      <span v-if="execution.usage"> · Token：{{ execution.usage.prompt_tokens ?? '未提供' }} 输入 / {{ execution.usage.completion_tokens ?? '未提供' }} 输出</span>
    </p>
    <ol aria-label="工具与子任务执行记录">
      <li v-for="event in execution.trace" :key="event.sequence" :class="{ child: event.agent_id !== 'main', failed: event.kind === 'tool_failed' }">
        <span class="agent-name">{{ event.agent_id === 'main' ? '助教' : '子任务 ' + event.agent_id.replace('child-', '') }}</span>
        <span>{{ event.message }}<small v-if="event.duration_ms != null"> · {{ seconds(event.duration_ms) }}</small></span>
      </li>
    </ol>
    <p v-if="execution.limited">已达到执行上限，答案基于本次已收集的资料。</p>
  </details>
</template>

<style scoped>
.agent-trace { margin: 12px 0; padding: 12px 16px; background: var(--el-fill-color-light); border-radius: 8px; font-size: 13px; overflow-wrap: anywhere; }
summary { cursor: pointer; color: var(--el-text-color-secondary); }
.metrics, small { color: var(--el-text-color-secondary); }
ol { margin: 12px 0 0; padding: 0; list-style: none; }
li { display: flex; gap: 12px; margin: 7px 0; }
.child { padding-left: 12px; border-left: 2px solid var(--el-border-color); }
.failed { color: var(--el-color-warning-dark-2); }
.agent-name { flex-shrink: 0; color: var(--el-text-color-secondary); }
</style>
