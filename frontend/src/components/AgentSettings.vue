<script setup lang="ts">
defineProps<{ modelValue: any, skills: any[] }>()
const titles: Record<string, string> = {
  'course-evidence': '课程证据检索', 'calculus-verification': '微积分计算核验',
  'document-comparison': '讲义与试卷分析', sympy: '符号数学方法',
  'scientific-critical-thinking': '科学证据与推理分析',
}
const compatibilityLabels: Record<string, string> = {
  compatible: '支持', limited: '部分能力支持', incompatible: '缺少运行能力',
}
const toolTitles: Record<string, string> = {
  search_knowledge: '课程资料检索', read_skill: '读取方法', read_skill_reference: '读取参考文件',
  calculate: '受限数学计算', spawn_agent: '子任务协作',
}
</script>

<template>
  <div class="agent-settings">
    <el-form-item label="自主学习助教（Agent）">
      <el-switch v-model="modelValue.enabled" active-text="由模型判断是否使用工具和子 agent" />
      <p class="form-help">需要支持 Tool Use 的生成模型。保存并发布后生效，演示模型只展示检索证据。</p>
    </el-form-item>
    <template v-if="modelValue.enabled">
      <el-form-item label="可用 Skills">
        <el-select v-model="modelValue.skills" multiple class="full-width" placeholder="选择模型可按需读取的学习方法">
          <el-option v-for="skill in skills" :key="skill.name" :value="skill.name"
            :label="titles[skill.name] || skill.name" :disabled="skill.compatibility?.status === 'incompatible'">
            <span>{{ titles[skill.name] || skill.name }}</span>
            <span v-if="skill.compatibility?.status !== 'compatible'" class="skill-status">
              {{ compatibilityLabels[skill.compatibility?.status] || '' }}
            </span>
          </el-option>
        </el-select>
        <p class="form-help">模型按问题选择方法，可补充检索、核验计算，并将独立问题交给子 agent。发布时固定所选方法和参考资料的版本，后续导入更新需重新发布才会生效。</p>
        <p v-for="skill in skills.filter(s => modelValue.skills.includes(s.name) && s.compatibility?.status !== 'compatible')"
          :key="skill.name" class="form-help skill-limit" role="status">
          <b>{{ titles[skill.name] || skill.name }} · {{ compatibilityLabels[skill.compatibility?.status] }}</b>：{{ skill.runtime_note }}
          <span v-if="skill.compatibility?.missing_tools?.length">缺少：{{ skill.compatibility.missing_tools.map((name: string) => toolTitles[name] || name).join('、') }}</span>
        </p>
      </el-form-item>
      <details>
        <summary>执行上限与 Skill 说明</summary>
        <el-form-item label="最多子 agent 数量（0 表示关闭拆分）">
          <el-input-number v-model="modelValue.max_subagents" :min="0" :max="5" />
        </el-form-item>
        <el-form-item v-if="modelValue.max_subagents > 0" label="同时执行的子 agent 上限">
          <el-input-number v-model="modelValue.max_parallel_subagents" :min="1" :max="5" />
        </el-form-item>
        <el-form-item label="主 agent 最多执行轮次">
          <el-input-number v-model="modelValue.max_steps" :min="1" :max="12" />
        </el-form-item>
        <el-form-item label="主、子 agent 合计工具调用上限">
          <el-input-number v-model="modelValue.max_tool_calls" :min="1" :max="48" />
        </el-form-item>
        <el-form-item label="Agent 执行超时（秒）">
          <el-input-number v-model="modelValue.timeout_seconds" :min="15" :max="600" />
        </el-form-item>
        <div v-for="skill in skills.filter(s => modelValue.skills.includes(s.name))" :key="skill.name" class="form-help">
          <p><b>{{ titles[skill.name] || skill.name }}</b>：{{ skill.description }}</p>
          <p v-if="skill.runtime_note">{{ skill.runtime_note }}</p>
          <p v-if="skill.requirements?.length">所需能力：{{ skill.requirements.map((name: string) => toolTitles[name] || name).join('、') }}</p>
          <p v-if="skill.sha256">当前文件版本：<code>{{ skill.sha256.slice(0, 12) }}</code></p>
        </div>
      </details>
    </template>
  </div>
</template>

<style scoped>
.agent-settings { margin: 24px 0; padding: 18px; border: 1px solid var(--el-border-color); border-radius: 10px; }
summary { cursor: pointer; margin-bottom: 18px; color: var(--el-text-color-secondary); }
.skill-status { float: right; margin-left: 16px; color: var(--el-text-color-secondary); font-size: 12px; }
.skill-limit { border-left: 3px solid var(--el-color-warning); padding-left: 10px; }
</style>
