# T+1 大模型选股分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于 MySQL 中已有 A 股三年历史日线数据，剔除 ST/退市股，通过技术因子预筛和本地大模型复核，输出 T+1 可能上涨的股票候选清单。

**Architecture:** 新增分析模块读取 `stock_basic` 与 `stock_daily`，先过滤 ST/退市股票，再计算近 60 个交易日的动量、均线、量能、波动和回撤等技术因子，形成可解释的候选池。候选池提交到 OpenAI 兼容本地大模型接口复核，要求模型返回 JSON，程序解析、落库并导出报告。

**Tech Stack:** Python 3.12、PyMySQL、pandas、requests、本地 OpenAI 兼容接口 `http://127.0.0.1:8317/v1`、模型 `gpt-5.5`。

---

## 需求规格

- 数据来源：当前 MySQL `astocks_collector` 中的 `stock_basic` 和 `stock_daily`。
- ST 剔除：股票名称包含 `ST`、`*ST`、`退`、`退市` 等风险标识的股票不得进入候选。
- 分析日期：默认使用 `stock_daily` 最新交易日。
- 量化预筛：计算趋势、动量、量能、波动、换手、近期涨跌等特征，先选出 Top N 候选。
- 大模型配置：`base_url=http://127.0.0.1:8317/v1`，`model=gpt-5.5`，`api_key=your-api-key-1`。
- 输出：最终选出 T+1 可能上涨的股票，包含代码、名称、评分、理由、风险。
- 落库：新增 `stock_analysis_pick` 表保存每次分析结果。
- 命令：新增 `astocks-collector analyze-t1`。

## 文件结构

- Modify: `.env`、`.env.example`：增加 LLM 与分析默认配置。
- Modify: `requirements.txt`、`pyproject.toml`：增加 `requests` 显式依赖。
- Modify: `src/astocks_collector/config.py`：增加 LLM 与分析参数。
- Modify: `src/astocks_collector/db.py`：增加分析结果表和写入方法。
- Create: `src/astocks_collector/analysis.py`：读取数据、剔除 ST、计算因子、调用 LLM、落库。
- Create: `src/astocks_collector/llm_client.py`：OpenAI 兼容 Chat Completions 客户端。
- Modify: `src/astocks_collector/cli.py`：增加 `analyze-t1` 命令。
- Modify: `README.md`：补充大模型分析配置和运行方式。

### Task 1: 配置和表结构

- [x] **Step 1: 增加 LLM 配置**

`.env` 与 `.env.example` 增加：

```ini
LLM_BASE_URL=http://127.0.0.1:8317/v1
LLM_MODEL=gpt-5.5
LLM_API_KEY=your-api-key-1
LLM_TIMEOUT_SECONDS=120
ANALYSIS_LOOKBACK_DAYS=90
ANALYSIS_PRESELECT_LIMIT=80
ANALYSIS_FINAL_LIMIT=10
```

- [x] **Step 2: 增加分析结果表**

`stock_analysis_pick` 保存分析日期、排名、股票、量化评分、LLM 评分、综合评分、理由、风险、原始模型 JSON。

### Task 2: 分析引擎

- [x] **Step 1: 读取候选数据**

按最新交易日读取非 ST 股票最近 90 天日线，要求每只股票至少 30 条记录。

- [x] **Step 2: 计算技术因子**

计算近 5/10/20 日收益、20 日均线偏离、5 日量比、20 日波动率、最大回撤、最新涨幅、换手率等。

- [x] **Step 3: 量化预筛**

用加权规则生成 `quant_score`，过滤异常涨跌停和过高波动，得到 Top 80。

### Task 3: LLM 复核和输出

- [x] **Step 1: 实现 LLM 客户端**

调用 `/chat/completions`，读取 `choices[0].message.content`，处理超时、HTTP 错误和非 JSON 内容。

- [x] **Step 2: 设计 JSON Prompt**

要求模型只从候选池中选择，不能编造股票，输出 JSON 数组。

- [x] **Step 3: 合并评分**

综合 `quant_score` 和 `llm_score` 排序，输出 Top 10。

### Task 4: CLI、文档和验证

- [x] **Step 1: 新增 CLI**

`astocks-collector analyze-t1 --final-limit 10` 输出候选并写入数据库。

- [x] **Step 2: 更新 README**

记录配置、命令、输出含义和风险声明。

- [x] **Step 3: 验证**

执行 `python -m compileall src`，执行 `astocks-collector analyze-t1 --preselect-limit 20 --final-limit 5`，确认剔除 ST、调用 LLM、写入 `stock_analysis_pick`。

## 自检

- 规格覆盖：ST 剔除、历史数据读取、LLM 配置、T+1 候选输出、落库、文档均已覆盖。
- 占位扫描：无 TBD/TODO/稍后实现。
- 风险声明：输出为模型和技术因子分析，不保证涨跌。
