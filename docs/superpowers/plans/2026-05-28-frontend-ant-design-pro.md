# A股数据前端页面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个使用 Ant Design Pro Components 和 AntV 图表的 A 股数据前端页面，展示基础信息、分段统计、历史行情和 T+1 分析结果。

**Architecture:** 后端新增 FastAPI 只读接口，从现有 MySQL 表读取真实数据；前端新增 Vite + React + TypeScript 工程，使用 `@ant-design/pro-components` 搭建企业级布局与表格，使用 `@antv/g2plot` 渲染行情和分段图表。前端通过 Vite 代理访问 `/api/*`，页面不直接连接数据库。

**Tech Stack:** Python 3.12、FastAPI、Uvicorn、React 18、TypeScript、Vite、Ant Design、Ant Design Pro Components、AntV G2Plot。

---

## 需求规格

- 前端必须使用 Ant Design Pro 体系：`ProLayout`、`PageContainer`、`ProTable`、`StatisticCard`。
- 图表必须使用 AntV：`@antv/g2plot` 的折线图、柱状图、饼图。
- 页面必须展示：
  - A 股基础信息：股票代码、名称、交易所、是否 ST、最新收盘价、最新涨跌幅、换手率。
  - 分段数据：交易所分布、ST/非 ST 分布、最新涨跌幅分布、成交额 Top。
  - 历史数据：选择股票后展示近 N 日 K 线简化折线和成交量柱状图。
  - 分析数据：展示 `stock_analysis_pick` 的 T+1 大模型选股结果。
- 数据必须来自现有 MySQL，不使用硬编码假数据。
- 文档必须记录启动后端和前端的方法。

## 文件结构

- Modify: `requirements.txt`、`pyproject.toml`：增加 `fastapi`、`uvicorn`。
- Create: `src/astocks_collector/web_api.py`：FastAPI 应用和只读数据接口。
- Modify: `src/astocks_collector/cli.py`：新增 `serve-api` 命令。
- Create: `web/package.json`、`web/tsconfig.json`、`web/vite.config.ts`、`web/index.html`。
- Create: `web/src/main.tsx`、`web/src/App.tsx`、`web/src/api.ts`、`web/src/styles.css`。
- Create: `web/src/components/AntvChart.tsx`：AntV G2Plot 生命周期封装。
- Modify: `README.md`：补充前端启动与页面说明。

### Task 1: 后端 API

- [x] **Step 1: 实现 FastAPI 应用**

创建 `/api/overview`、`/api/stocks`、`/api/segments`、`/api/history/{symbol}`、`/api/analysis`。

- [x] **Step 2: 接入 CLI**

新增 `astocks-collector serve-api --host 127.0.0.1 --port 8000`。

### Task 2: 前端工程

- [x] **Step 1: 创建 Vite React TypeScript 工程**

使用 Ant Design Pro Components 和 AntV G2Plot 依赖。

- [x] **Step 2: 实现 API 客户端**

统一封装 `/api/*` 请求和类型。

### Task 3: 页面实现

- [x] **Step 1: 实现整体布局**

使用 `ProLayout` 和 `PageContainer`，页面首屏为工作台，不做营销页。

- [x] **Step 2: 实现数据区块**

使用统计卡、ProTable、分段图表、历史图表和分析结果表。

### Task 4: 验证和文档

- [x] **Step 1: 构建验证**

执行后端编译和前端 `npm run build`。

- [x] **Step 2: 浏览器验证**

启动后端和前端，用浏览器打开页面，确认数据、图表、表格可渲染。

验证记录：

- 2026-05-28 首轮 Playwright 已生成 `output/playwright/overview.png`、`stocks.png`、`segments.png`、`history.png`、`analysis.png`，页面曾成功读取 MySQL 真实数据。
- 后续统一复权口径和错误处理后，`192.168.50.19:3306` 连续 6 次连接超时，最新版真实数据页面复验暂时受阻；API 健康检查可用，数据库不可达时返回 503。
- 2026-05-28 再次优化降级逻辑：前端接口失败时显示明确告警和空值，不再把概览误显示为 0；Playwright 生成 `output/playwright/offline-overview.png`，确认数据库不可达时页面可读且有告警。
- 2026-05-28 最新 MySQL 连通性复查仍失败：`192.168.50.19:3306` 连续 3 次 socket 连接超时；真实数据 E2E 仍需等待外部数据库网络恢复。
- 2026-05-28 最新连续目标推进复查：`192.168.50.19:3306` 仍连续 3 次 socket 连接超时；`python -m compileall src` 和 `npm run build` 通过；API 健康检查通过，业务接口按预期返回 503；Playwright 生成 `output/playwright/offline-overview-latest.png`，确认前端显示不可用告警且未展示假 0。
- 2026-05-28 新增可重复验收脚本：`scripts/verify_frontend.ps1` 会编译后端、构建前端、启动 API/Vite、自动判断 MySQL 在线/离线并调用 `web/scripts/verify_frontend.mjs` 生成 Playwright 截图；已用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify_frontend.ps1 -SkipBuild` 验证离线分支通过，且进程清理后 `8000/5173` 无监听残留。
- 2026-05-28 增强验收脚本覆盖：离线模式逐个校验 `/api/overview`、`/api/stocks`、`/api/segments`、`/api/history/000001?days=120`、`/api/analysis` 返回 503，并截图五个菜单页面；在线模式会校验五个 API 都返回真实数据、分段/历史图表 canvas 已渲染、分析候选出现在页面中。已用 Windows PowerShell 5 跑通 `-SkipBuild` 离线分支。
- 2026-05-28 用户指定改用本机 WSL Docker MySQL 8；已创建 `astocks-mysql8` 容器，`.env` 切换到 `127.0.0.1:3306`，同步 `stock_basic` 5524 条、回填 51 只重点股票近一年日线 12317 行，并用 `analyze-t1 --no-llm` 写入 10 条分析结果。
- 2026-05-28 完整在线验收通过：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify_frontend.ps1` 完成后端编译、前端构建、五个 API 真实数据校验、五个页面 Playwright 截图、分段/历史 AntV canvas 渲染校验；脚本结束后 API/Vite/Playwright 进程和 `8000/5173` 监听均为 0。

- [x] **Step 3: 更新 README**

记录依赖安装、后端启动、前端启动、页面功能和接口。

## 自检

- 规格覆盖：Ant Design Pro、AntV、基础信息、分段数据、历史数据、分析数据均有任务覆盖。
- 占位扫描：无 TBD/TODO/稍后实现。
- 数据来源：所有页面数据通过 FastAPI 从 MySQL 查询。
