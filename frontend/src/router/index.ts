import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '../views/HomeView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
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
  ],
})

export default router
