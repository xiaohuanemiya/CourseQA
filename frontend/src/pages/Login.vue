<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, session, action } from '../api'
const form=reactive({username:'',password:''}), register=ref(false),busy=ref(false),route=useRoute(),router=useRouter()
async function submit(){busy.value=true;await action(async()=>{session.user=await api('/auth/'+(register.value?'register':'login'),'POST',form);const next=String(route.query.next||'/');router.push(next.startsWith('/')&&!next.startsWith('//')?next:'/')});busy.value=false}
</script>
<template><section class="login-page"><div class="page-eyebrow">A SPACE FOR UNDERSTANDING</div><h1>{{register?'开始你的学习旅程':'欢迎回到课程问答平台'}}</h1><p class="muted">登录后保存课程问答和练习记录。</p><el-form label-position="top" @submit.prevent="submit"><el-form-item label="用户名"><el-input v-model="form.username" placeholder="字母、数字或下划线" autocomplete="username"/></el-form-item><el-form-item label="密码"><el-input v-model="form.password" type="password" show-password :autocomplete="register?'new-password':'current-password'" placeholder="至少 8 个字符"/></el-form-item><el-button type="primary" native-type="submit" :loading="busy" class="full-width">{{register?'创建账号':'登录'}}</el-button></el-form><button class="text-link" @click="register=!register">{{register?'已有账号？返回登录':'还没有账号？立即注册'}} →</button></section></template>
