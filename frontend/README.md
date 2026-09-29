# 平台前端

Vue 3 + TypeScript + Vite。提供部门工作平台外壳：工作台、数据分析（内嵌 SmartLabHub）、钉钉听记内容标准化处理、6S 标准化报告。

```bash
npm install
npm run dev      # 开发服务，/api 代理到 http://localhost:8000
npm run build    # 类型检查 + 生产构建
```

目录：

```text
src/
├── api/          # 后端接口封装（http.ts / user.ts / dashboard.ts）
├── components/   # DataAnalysis.vue：挂载 SmartLabHub 原生界面
├── App.vue       # 侧栏导航、工作台与模块入口
└── style.css     # 平台视觉规范（深蓝侧栏 + 卡片式工作区 + 橙色强调）
```

项目总体说明见根目录 `README.md`。
