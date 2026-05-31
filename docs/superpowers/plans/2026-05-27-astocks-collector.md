# A股数据采集器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建可上线运行的 A 股日线数据采集器，支持 MySQL 建库建表、历史回填、每日增量采集和常驻定时调度。

**Architecture:** 采用 Python 命令行应用，配置层读取 `.env` 与环境变量，数据源层封装 AKShare，数据库层使用 PyMySQL 执行幂等 upsert，任务层编排股票列表同步、历史回填、增量采集和 APScheduler 定时运行。所有写库操作都通过唯一键和 `ON DUPLICATE KEY UPDATE` 保持可重复执行。

**Tech Stack:** Python 3.12、AKShare、pandas、PyMySQL、APScheduler、python-dotenv、MySQL 8.x/兼容版本。

---

## 需求规格

- MySQL 目标地址：`192.168.50.19:3306`，用户 `root`，密码 `root`。
- 默认数据库名：`astocks_collector`，程序首次运行时自动创建。
- 采集范围：沪深京 A 股，股票列表来自 `ak.stock_zh_a_spot_em()`。
- 历史行情：来自 `ak.stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust)`。
- 历史 3 年：`backfill --years 3` 自动计算最近 3 年窗口并回填。
- 日线数据源：默认使用 `ak.stock_zh_a_daily()`，可切换为 Eastmoney 或自动降级。
- 回填性能：`ASTOCKS_MAX_WORKERS` 控制并发采集进程数，默认 2。
- 默认复权口径：`qfq`，可通过环境变量或命令行切换为不复权/后复权。
- 每日增量：收盘后定时拉取最近日期窗口，插入或更新到 `stock_daily`。
- 幂等要求：重复运行不能产生重复行情行。
- 可运维要求：提供 README、配置模板、命令行入口、日志、任务运行记录和失败明细。

## 文件结构

- `pyproject.toml`：声明包元数据、依赖和命令行入口。
- `requirements.txt`：提供直接 pip 安装方式。
- `.gitignore`：排除 `.env`、缓存、日志和本地虚拟环境。
- `.env`：本地运行配置，使用用户提供的 MySQL 地址与账号。
- `.env.example`：配置模板。
- `src/astocks_collector/config.py`：集中读取和校验配置。
- `src/astocks_collector/db.py`：负责建库建表、连接、批量 upsert、运行日志。
- `src/astocks_collector/market_data.py`：负责 AKShare 股票列表与日线数据适配。
- `src/astocks_collector/collector.py`：负责同步股票、历史回填、增量采集编排。
- `src/astocks_collector/scheduler.py`：负责 APScheduler 定时任务。
- `src/astocks_collector/cli.py`：提供 `init-db`、`sync-basic`、`backfill`、`incremental`、`scheduler` 命令。
- `README.md`：记录安装、配置、建表、采集、调度和表结构说明。

### Task 1: 项目骨架与配置

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `.env`
- Create: `.env.example`
- Create: `src/astocks_collector/__init__.py`
- Create: `src/astocks_collector/config.py`

- [x] **Step 1: 创建依赖和入口**

创建 `pyproject.toml`，声明依赖与 `astocks-collector` 命令。

- [x] **Step 2: 创建配置文件**

创建 `.env` 与 `.env.example`，默认连接 `192.168.50.19`，数据库名为 `astocks_collector`，每日任务时间为北京时间 `17:30`。

- [x] **Step 3: 实现配置读取**

实现 `AppConfig.from_env()`，读取 MySQL、采集窗口、限速、重试、调度时间等配置，并提供 `mysql_without_db` 与 `mysql_with_db` 两种连接参数。

### Task 2: MySQL Schema 与写库

**Files:**
- Create: `src/astocks_collector/db.py`

- [x] **Step 1: 实现连接管理**

提供无库连接用于 `CREATE DATABASE IF NOT EXISTS`，有库连接用于建表与写数据。

- [x] **Step 2: 实现表结构**

创建 `stock_basic`、`stock_daily`、`collector_run`、`collector_error` 四张表。

- [x] **Step 3: 实现幂等写入**

股票基础信息按 `symbol` upsert；日线按 `(symbol, trade_date, adjust_type)` upsert；任务日志记录开始、结束、状态和计数。

### Task 3: AKShare 数据源适配

**Files:**
- Create: `src/astocks_collector/market_data.py`

- [x] **Step 1: 适配股票列表**

从 `stock_zh_a_spot_em()` 读取 `代码`、`名称` 等字段，推断交易所并标准化为内部结构。

- [x] **Step 2: 适配日线行情**

从 `stock_zh_a_hist()` 读取 `日期`、`开盘`、`收盘`、`最高`、`最低`、`成交量`、`成交额`、`振幅`、`涨跌幅`、`涨跌额`、`换手率`，转成数据库字段。

- [x] **Step 3: 控制数据源压力**

单股票采集失败要记录错误并继续；每只股票之间按配置 sleep，避免短时间高频请求。

### Task 4: 采集编排与命令行

**Files:**
- Create: `src/astocks_collector/collector.py`
- Create: `src/astocks_collector/cli.py`

- [x] **Step 1: 实现建表命令**

`python -m astocks_collector.cli init-db` 创建数据库与表。

- [x] **Step 2: 实现基础信息同步**

`python -m astocks_collector.cli sync-basic` 同步沪深京 A 股列表。

- [x] **Step 3: 实现历史回填**

`python -m astocks_collector.cli backfill --start-date 19900101 --end-date 20260527` 支持全量或 `--symbols 000001,600000` 小范围回填。

- [x] **Step 4: 实现每日增量**

`python -m astocks_collector.cli incremental --days 10` 按最近窗口拉取并 upsert。

### Task 5: 定时调度与文档

**Files:**
- Create: `src/astocks_collector/scheduler.py`
- Create: `README.md`

- [x] **Step 1: 实现常驻调度**

`python -m astocks_collector.cli scheduler` 启动 APScheduler，每天北京时间配置时间执行增量任务。

- [x] **Step 2: 更新 README**

说明功能、表结构、安装、配置、建库、采集、定时运行、Windows 任务计划程序示例和运维注意事项。

### Task 6: 验证与资源回收

**Files:**
- Verify only

- [ ] **Step 1: 安装依赖**

执行 `python -m pip install -r requirements.txt`。

- [ ] **Step 2: 语法验证**

执行 `python -m compileall src`，预期退出码为 0。

- [ ] **Step 3: 建库建表**

执行 `python -m astocks_collector.cli init-db`，预期 MySQL 中存在 `astocks_collector` 与四张表。

- [ ] **Step 4: 小样本采集验证**

执行 `python -m astocks_collector.cli backfill --symbols 000001 --start-date 20260520 --end-date 20260527`，验证可写入 `stock_daily`。

- [ ] **Step 5: 最新增量验证**

执行 `python -m astocks_collector.cli incremental --symbols 000001 --days 10`，验证重复运行不产生重复键。

- [ ] **Step 6: 清理残留进程**

确认未遗留常驻 `python` 调度进程；本任务不启动长期服务。

## 自检

- 规格覆盖：建库建表、历史回填、每日增量、定时调度、配置和 README 均有任务覆盖。
- 占位扫描：计划中未保留 TBD/TODO/稍后实现等占位项。
- 类型一致性：内部统一使用 `symbol`、`trade_date`、`adjust_type` 作为核心维度。
