<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, session, action } from './api'
import { ArrowRight, Collection, Grid, Setting, SwitchButton, Timer, Connection } from '@element-plus/icons-vue'
const route=useRoute(), router=useRouter()
const isAdmin=computed(()=>route.path.startsWith('/admin'))
async function logout(){await action(async()=>{await api('/auth/logout','POST');session.user=null;router.push('/')})}
</script>
<template>
  <div class="shell">
    <aside class="sidebar">
      <router-link to="/" class="brand"><span class="brand-mark"><el-icon :size="25"><Collection/></el-icon></span><span>课程问答平台<small>COURSE Q&amp;A</small></span></router-link>
      <div class="nav-caption">学习空间</div>
      <router-link to="/" class="nav-link"><el-icon><Grid/></el-icon> 应用广场 <span class="nav-dot"></span></router-link>
      <template v-if="session.user?.role==='admin'">
        <div class="nav-caption spaced">管理工作台</div>
        <router-link to="/admin/apps" class="nav-link"><el-icon><Grid/></el-icon> 应用管理</router-link>
        <router-link to="/admin/knowledge-bases" class="nav-link"><el-icon><Collection/></el-icon> 知识库</router-link>
        <router-link to="/admin/graph" class="nav-link"><el-icon><Connection/></el-icon> 图数据库</router-link>
        <router-link to="/admin/review-plans" class="nav-link"><el-icon><Collection/></el-icon> 复习计划</router-link>
        <router-link to="/admin/models" class="nav-link"><el-icon><Setting/></el-icon> 模型与连接</router-link>
        <router-link to="/admin/tasks" class="nav-link"><el-icon><Timer/></el-icon> 任务中心</router-link>
      </template>
      <div class="sidebar-bottom"><div class="small-label">学习，从理解开始。</div><p>让每一个答案<br>都有迹可循。</p><span class="powered">POWERED BY SEMANTICA</span></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><div class="breadcrumb">课程问答 <el-icon><ArrowRight/></el-icon> {{isAdmin?'管理工作台':'学习空间'}}</div><div class="account"><span class="live-dot"></span><span v-if="session.user">{{session.user.username}} <small>{{session.user.role==='admin'?'管理员':'学习者'}}</small></span><el-button v-if="session.user" text :icon="SwitchButton" aria-label="退出登录" @click="logout"/><router-link v-else to="/login" class="login-link">登录 / 注册 <span>↗</span></router-link></div></header>
      <main><router-view :key="route.path"/></main>
      <footer>COURSE Q&amp;A <span>知识有来源 · 学习有方向</span></footer>
    </div>
  </div>
</template>
