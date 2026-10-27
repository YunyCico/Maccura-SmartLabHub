# 开发与验证工具

## make_sample_zip.py — 生成解析样本

造一个与《对外数据拉取接口（文件下载版）》8.2 节**完全一致**的 ZIP：
`tables/*.columns.json` + `tables/*.csv` + `metrics/*.json` + `combinations/*.json` + `manifest.json`，
用于在数据权限未开通时验证解析链路和前端展示效果。

```bash
.venv/Scripts/python.exe tools/make_sample_zip.py
# 输出：data/_sample/sample.zip
```

带分页统计的版本：`python -c "from tools.make_sample_zip import build; build(with_paging=True)"`

页面上「预览解析效果（本地样本）」按钮调用的就是这个文件。

## verify_ui.mjs — CDP 截图验收

起无头 Chrome 打开页面，依次截图 5 个关键状态
（初始页 / 条件编辑器 / 解析结果 / 字段画像 / 供应商参数），并回报页面 JS 报错。

```bash
node tools/verify_ui.mjs shots
```

前置：后端已在 8100 端口运行；Chrome 存在于
`%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`。

## 测试

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

全部离线，不依赖数据中台连通性。覆盖：

- ZIP 目录结构解析（tables/metrics/combinations/manifest）
- `columns.json` 中文字段注释与 SQL 类型挂载
- GBK 中文 CSV 编码兜底
- 分页规则（单传 pageNo 拒绝、pageSize 边界 1/10000）
- condition 操作符枚举、字段名合法性正则、`in` 需填值
- Token 缓存 JWT exp 解析
- **接口层 TestClient**（打桩外部调用）：condition key 不匹配返回 400 而非 500
- 固定配置不可被页面改坏

基线：**37 passed**。

## 前端开发

```bash
cd frontend
npm run dev     # 端口 5180，/api 自动代理到 127.0.0.1:8100
npm run build   # 构建到 dist，后端启动时自动挂载
```

> npm 源已设为 `https://registry.npmmirror.com`（默认源在本机极慢，实测 6 分钟未装完）。