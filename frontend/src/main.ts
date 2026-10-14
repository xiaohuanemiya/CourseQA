import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import 'katex/dist/katex.min.css'
import App from './App.vue'
const Home = () => import('./pages/Home.vue')
const Login = () => import('./pages/Login.vue')
const Study = () => import('./pages/Study.vue')
const AdminApps = () => import('./pages/AdminApps.vue')
const KnowledgeBases = () => import('./pages/KnowledgeBases.vue')
const KnowledgeEditor = () => import('./pages/KnowledgeEditor.vue')
const Models = () => import('./pages/Models.vue')
const Tasks = () => import('./pages/Tasks.vue')
const GraphExplorer = () => import('./pages/GraphExplorer.vue')
const AdminReviewPlans = () => import('./pages/AdminReviewPlans.vue')
import { session, refreshSession } from './api'
import './style.css'

export const router = createRouter({history: createWebHistory(), routes: [
  {path:'/',component:Home}, {path:'/login',component:Login},
  {path:'/apps/:id',component:Study,meta:{auth:true}},
  {path:'/admin/apps',component:AdminApps,meta:{admin:true}},
  {path:'/admin/knowledge-bases',component:KnowledgeBases,meta:{admin:true}},
  {path:'/admin/knowledge-bases/:id',component:KnowledgeEditor,meta:{admin:true}},
  {path:'/admin/models',component:Models,meta:{admin:true}},
  {path:'/admin/tasks',component:Tasks,meta:{admin:true}},
  {path:'/admin/graph',component:GraphExplorer,meta:{admin:true}},
  {path:'/admin/review-plans',component:AdminReviewPlans,meta:{admin:true}},
  {path:'/:pathMatch(.*)*',redirect:'/'}
]})
router.beforeEach(async to => {
  if (!session.ready) await refreshSession()
  if ((to.meta.auth || to.meta.admin) && !session.user) return {path:'/login',query:{next:to.fullPath}}
  if (to.meta.admin && session.user?.role !== 'admin') return '/'
})
createApp(App).use(router).use(ElementPlus,{locale:zhCn}).mount('#app')
