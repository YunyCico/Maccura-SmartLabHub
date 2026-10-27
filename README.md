# Maccura-SmartLabHub

迈克生物集团 · 实验室运营管理部 —— 部门工作平台（钉钉企业内部应用）。

平台外壳为 Vue 3 + FastAPI，「数据分析」「服务需求报表」「6S标准报告」「Ding听记优化」等工作工具模块。

## 目录结构

层级关系：**主页 → 部门工作模块 → 各模块文件夹**。

```text
SmartLabHub/
├── home/                       # 主页（Vue 3 + TypeScript）
│   ├── src/
│   │   ├── views/HomeView.vue  # 主页：今日待办 / 部门工作模块 / 分析报表
│   │   ├── components/         # DataAnalysis.vue（数据分析嵌入页）、LisDataOrganizer.vue
│   │   ├── api/  router/  lib/ # 接口封装、路由、钉钉授权
│   │   └── style.css           # 平台视觉规范
│   └── vite.config.ts          # 开发代理 /api -> :8000
│
├── modules/                    # 部门工作模块（每个模块一个文件夹）
│   ├── data_analysis/          # 数据分析（已上线）
│   │   ├── smartlab_engine.py  # 数据源、字段字典、汇总、关联、透视、导出、原始编辑
│   │   ├── excel_reader.py     # xls/xlsx/csv/html 统一按内容读取
│   │   ├── table_format.py     # 按文件内容嗅探真实格式
│   │   ├── report_templates.py # 报告计算
│   │   ├── report_render.py# 报告渲染为单文件 HTML
│   │   ├── web/                # 本模块界面（数据分析页 iframe 嵌入它，端口 8765）
│   │   └── data/               # 本模块运行期数据，已忽略入库
│   │       ├── smartlab/       #   library / exports / reports
│   │       ├── lis/            #   LIS 数据整理：staging / standardized / mappings
│   │       └── imports/        #   通用导入暂存
│   ├── service_demand/         # 服务需求报表（已上线，原 DataFetchPortal）
│   │   ├── backend/            # FastAPI 子应用，包名 service_portal（避免与平台 app 重名）
│   │   ├── frontend/           # Vue 界面；dist 已入库，服务器不需要装 Node
│   │   ├── config.json         # 内网账号与 openApiKey，**不入库**（首次在页面里填）
│   │   └── data/               # 拉取历史与样例，已忽略入库
│   ├── dingtalk-minutes/       # 钉钉听记内容标准化处理（规划中）
│   ├── 6s-report/              # 6S 标准化报告（规划中）
│   └── service-report/         # 服务报告标准化（规划中）
│
├── platform/                   # 平台公共层
│   ├── backend/                # FastAPI 后端
│   │   ├── app/
│   │   │   ├── api/            # 路由：health / user / dashboard / data_analysis
│   │   │   ├── core/           # 配置与数据库
│   │   │   └── services/       # 引擎封装、LIS 数据整理、通用导入
│   │   └── requirements.txt
│   └── tools/                  # 启动/停止脚本、验证脚本、索引重建工具
│
├── tests/                      # pytest 全模块回归
├── deployment/                 # docker-compose（正式部署）
└── backup/ logs/ shots/        # 备份、运行日志、验证截图
```

## 主页区域

| 区域 | 内容 |
| --- | --- |
| 今日待办 | 汇总 Salesforce / 公司 OA / DingDing 的待处理消息，可勾选完成 |
| 部门工作模块 | 数据分析、钉钉听记内容标准化处理、6S 标准化报告、服务报告标准化 |
| 分析报表 | 各模块的统计与报表区域 |

## 功能模块

| 模块          | 状态  | 说明                         |
| ----------- | --- | -------------------------- |
| 工作台         | 可用  | 指标概览、最近任务、数据质量提醒、模块入口      |
| 数据分析        | 可用  | 内嵌 SmartLabHub v2.4.0 全部能力 |
| 钉钉听记内容标准化处理 | 规划中 | 听记文本清洗、要点抽取、标准字段整理         |
| 6S 标准化报告    | 规划中 | 检查记录、问题归集、整改跟踪、报告生成        |

数据分析内含：概览、数据源管理、原始数据预览与编辑（撤销重做、显式保存并备份）、字段字典（纵向列表 + 横向矩阵）、汇总提取（纵向合并/横向关联、筛选、字段映射、去重排序）、结果分析（逐列体检、数据透视）、分析报告（设备模块负载与堵塞点、免疫线实际测试速度证据链）、导出文件管理、AI 助手（OpenAI 兼容接口，**已暂时隐藏，代码保留待重新规划**）、设置。

## 本地运行

前置条件：Python 3.12、Node.js 20+。

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

- 平台：<http://localhost:5173/>
- 后端接口文档：<http://localhost:8000/docs>
- SmartLabHub 原生界面：<http://127.0.0.1:8765/>

## 测试

```bash
backend\.venv\Scripts\python.exe -m pytest -q
npm run build --prefix frontend
```

## 数据与安全约定

- 导入的原始文件复制到 `redata/data/smartlab/library/sources`，源文件不被修改。
- 原始数据编辑默认只改内存；写回数据源、导出、删除均需显式确认，覆盖前自动备份。
- AI 仅输出建议，不自动写入底表；接口密钥只保存在本机配置。
- `redata/data/`、`*.db`、`.env`、依赖与构建产物均已忽略入库。

## 分支约定

`main` 为稳定分支，功能通过 `feature/*` 分支以 PR 合入。
