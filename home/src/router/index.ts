import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'
import { authState, ensureAuthLoaded } from '../api/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue'),
      // 登录页不套外壳（没有顶部用户栏、没有退出按钮）
      meta: { title: '登录', code: '', public: true, bare: true },
    },
    {
      path: '/setup',
      name: 'setup',
      component: () => import('../views/SetupView.vue'),
      meta: { title: '首次设置', code: '', public: true, bare: true },
    },
    {
      path: '/',
      name: 'home',
      component: HomeView,
      meta: { title: 'SmartLabHub', code: '01' },
    },
    {
      path: '/data-analysis',
      name: 'data-analysis',
      component: { render: () => null },
      meta: { title: '数据分析', code: '02' },
    },
    {
      path: '/settings/integrations',
      name: 'settings-integrations',
      component: () => import('../views/IntegrationSettingsView.vue'),
      meta: { title: '消息互通设置', code: '02' },
    },
    {
      path: '/dingtalk-minutes',
      name: 'dingtalk-minutes',
      component: () => import('../views/PlannedModuleView.vue'),
      meta: {
        title: '钉钉听记内容标准化处理',
        code: '03',
        summary: '将钉钉听记的口语化文本清洗、抽取关键事项，统一人员 / 时间 / 问题 / 结论字段，并生成可归档的标准化记录。',
        points: ['听记文本清洗与要点抽取', '人员 / 时间 / 问题 / 结论字段标准化', '一键生成可归档记录'],
      },
    },
    {
      path: '/6s-report',
      name: '6s-report',
      component: () => import('../views/PlannedModuleView.vue'),
      meta: {
        title: '6S标准化报告',
        code: '04',
        summary: '整理 6S 现场检查记录、问题分类与整改跟踪，结合现场照片说明，自动生成标准化 6S 报告。',
        points: ['6S 检查记录与问题归集', '整改跟踪与闭环管理', '标准化报告自动生成'],
      },
    },
    {
      path: '/service-report',
      name: 'service-report',
      component: () => import('../views/PlannedModuleView.vue'),
      meta: {
        title: '服务报告标准化',
        code: '05',
        summary: '将各实验室上报的服务报告（安装、维修、巡检、培训等）清洗套版，统一结构化字段与排版，一键生成标准化服务报告。',
        points: ['服务报告模板套版与字段标准化', '多来源报告归集与查重', '一键生成标准排版报告'],
      },
    },
    {
      path: '/service-demand',
      name: 'service-demand',
      component: () => import('../views/ServiceDemandView.vue'),
      meta: { title: '服务需求报表', code: '05' },
    },
    {
      path: '/account',
      name: 'account',
      component: () => import('../views/AccountView.vue'),
      meta: { title: '账号管理', code: '06' },
    },
  ],
})

/**
 * 登录守卫。
 *
 * 顺序很重要：
 *  1. 还没取过登录态 -> 先问后端（每次页面加载只问一次）
 *  2. 平台还没有任何账号 -> 只允许进 /setup（首次引导）
 *  3. 没登录 -> 顶到 /login，并记住原地址
 *  4. 已登录还去 /login 或 /setup -> 回首页
 */
router.beforeEach(async (to) => {
  await ensureAuthLoaded()

  const isPublic = Boolean(to.meta.public)

  // 平台未初始化：只能进首次设置页
  if (!authState.initialized) {
    return to.name === 'setup' ? true : { name: 'setup' }
  }

  // 已初始化但没登录：只能进登录页
  if (!authState.user) {
    if (to.name === 'login') return true
    // 未初始化的场景已经被上面接走了，走到这里一定是要登录
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  // 已登录：登录页/设置页没有意义
  if (isPublic) return { name: 'home' }

  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || 'SmartLabHub'
  document.title = title === 'SmartLabHub' ? 'SmartLabHub' : `${title} · SmartLabHub`
})

export default router
