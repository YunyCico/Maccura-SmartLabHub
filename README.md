# Maccura-SmartLabHub

迈克生物集团 · 实验室运营管理部 —— 部门工作平台（钉钉企业内部应用）。

平台外壳为 Vue 3 + FastAPI，「数据分析」「6S标准报告」「Ding听记优化」等工作工具模块。

## 目录结构

```文本
Maccura-SmartLabHub/
├── backend/                  # FastAPI 平台后端
│   ├── app/
│   │   ├── api/              # 路由：health / user / dashboard / data_analysis
│   │   ├── core/             # 配置
│   │   ├── services/         # smartlab_service：封装 SmartLabHub 引擎
│   │   └── main.py           # 应用入口，启动时挂载 SmartLabHub 原生服务
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vue 3 + TypeScript 平台前端
│   ├── src/
│   │   ├── api/              # 后端接口封装
│   │   ├── components/       # DataAnalysis.vue（内嵌 SmartLabHub 界面）
│   │   ├── App.vue           # 工作台外壳与三大模块入口
│   │   └── style.css         # 平台视觉规范
│   └── vite.config.ts        # 开发代理 /api → :8000
├── smartlab_core/            # SmartLabHub 引擎（自压缩包整体迁入）
│   ├── smartlab_engine.py    # 数据源、字段字典、汇总、关联、透视、导出、AI、钉钉、原始编辑
│   ├── report_templates.py   # 报告计算：设备负载、免疫线速度证据链
│   ├── report_render.py      # 报告渲染为单文件 HTML
│   └── web/                  # SmartLabHub 原版界面（index.html / style.css / app.js / help.html）
├── deployment/
│   └── docker-compose.yml    # PostgreSQL（正式部署用；开发期使用 SQLite）
├── tests/                    # pytest：引擎解析、接口与工作台
└── data/                     # 运行期数据，已忽略入库
    └── smartlab/
        ├── library/          # 数据源副本、索引、结果池
        ├── exports/          # 导出文件与原始文件备份
        └── reports/          # 生成的 HTML 分析报告
```

## 功能模块

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 工作台 | 可用 | 指标概览、最近任务、数据质量提醒、模块入口 |
| 数据分析 | 可用 | 内嵌 SmartLabHub v2.4.0 全部能力 |
| 钉钉听记内容标准化处理 | 规划中 | 听记文本清洗、要点抽取、标准字段整理 |
| 6S 标准化报告 | 规划中 | 检查记录、问题归集、整改跟踪、报告生成 |

数据分析内含：概览、数据源管理（上传/文件夹扫描/钉钉目录）、原始数据预览与编辑（撤销重做、显式保存并备份）、字段字典（纵向列表 + 横向矩阵）、汇总提取（纵向合并/横向关联、筛选、字段映射、去重排序）、结果分析（逐列体检、数据透视）、分析报告（设备模块负载与堵塞点、免疫线实际测试速度证据链）、导出文件管理、AI 助手（OpenAI 兼容接口）、设置。

## 本地运行

前置：Python 3.12、Node.js 20+。

```bash
# 1. 后端依赖
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 2. 启动后端（同时拉起 SmartLabHub 原生界面服务 :8765）
cd ..
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000

# 3. 启动前端（另开一个终端）
npm install --prefix frontend
npm run dev --prefix frontend
```

访问：

- 平台：http://localhost:5173/
- 后端接口文档：http://localhost:8000/docs
- SmartLabHub 原生界面：http://127.0.0.1:8765/

## 测试

```bash
backend\.venv\Scripts\python.exe -m pytest -q
npm run build --prefix frontend
```

## 数据与安全约定

- 导入的原始文件复制到 `data/smartlab/library/sources`，源文件不被修改。
- 原始数据编辑默认只改内存；写回数据源、导出、删除均需显式确认，覆盖前自动备份。
- AI 仅输出建议，不自动写入底表；接口密钥只保存在本机配置。
- `data/`、`*.db`、`.env`、依赖与构建产物均已忽略入库。

## 分支约定

`main` 为稳定分支，功能通过 `feature/*` 分支以 PR 合入。
