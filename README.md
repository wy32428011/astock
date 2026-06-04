# A股数据采集器

本项目用于采集沪深京 A股股票基础信息、历史日线行情，并在每日收盘后定时采集最新日线数据写入 MySQL。

## 功能

- 自动创建 MySQL 数据库和业务表。
- 使用 AKShare 获取沪深京 A股股票列表。
- 使用 AKShare 获取单只股票日线历史行情。
- 使用 MySQL 唯一键和 `ON DUPLICATE KEY UPDATE` 保证重复运行不产生重复行情。
- 支持命令行历史回填、最近窗口增量采集、常驻定时调度。
- 记录每次采集运行结果和失败明细，便于排查数据源或网络问题。
- 提供 FastAPI 只读数据接口，供前端工作台查询真实 MySQL 数据。
- 提供 Ant Design Pro + AntV 前端页面，展示基础信息、分段数据、历史数据和分析数据。
- 提供实时行情分析和模拟交易账户，支持实时信号、模拟持仓、订单和成交记录。
- 实时交易页面支持自动刷新、自动模拟买卖，并可使用规则预筛 + LLM 复核生成最终决策；LLM 不可用时自动降级为规则引擎。
- 提供全 A 股未来 3 个交易日涨势分析，使用量化预筛 + LLM 复核输出短线候选，LLM 不可用时降级为量化排序。
- 提供 9:15-9:25 集合竞价分阶段采集与 LLM 快速选股，采集期融合行情快照，决策期输出候选，不自动下单。

## 数据源

- 股票列表：`ak.stock_zh_a_spot_em()`，一次返回沪深京 A股实时行情和代码列表。
- 日线行情：`ak.stock_zh_a_hist(symbol, period="daily", start_date, end_date, adjust)`。
- 备用股票列表：当 Eastmoney 实时行情接口断连时，自动降级到 `ak.stock_info_a_code_name()`。
- 备用日线行情：当 Eastmoney 日线接口断连时，自动降级到 `ak.stock_zh_a_daily()`。
- 默认复权口径：`qfq` 前复权，可通过配置或命令行切换。
- 默认日线数据源：`sina`，也可用 `--daily-provider auto` 自动降级。

AKShare 接口可能因上游页面变化而变动；如果出现字段缺失或连接被拒绝，优先查看 `collector_error` 表中的错误明细。

## 数据库配置

默认配置位于 `.env`：

```ini
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DATABASE=astocks_collector
MYSQL_CHARSET=utf8mb4

ASTOCKS_ADJUST=qfq
ASTOCKS_DAILY_PROVIDER=sina
ASTOCKS_DEFAULT_START_DATE=19900101
ASTOCKS_DAILY_WINDOW_DAYS=10
ASTOCKS_REQUEST_INTERVAL_SECONDS=0.35
ASTOCKS_MAX_RETRIES=3
ASTOCKS_MAX_WORKERS=2
ASTOCKS_BATCH_SIZE=500
ASTOCKS_SCHEDULE_TIME=17:30
ASTOCKS_TIMEZONE=Asia/Shanghai
LOG_LEVEL=INFO

LLM_BASE_URL=http://127.0.0.1:8317/v1
DOCKER_LLM_BASE_URL=http://172.24.192.1:8317/v1
LLM_MODEL=gpt-5.5
LLM_API_KEY=your-api-key-1
LLM_TIMEOUT_SECONDS=120
ANALYSIS_LOOKBACK_DAYS=90
ANALYSIS_PRESELECT_LIMIT=80
ANALYSIS_FINAL_LIMIT=10
T1_LLM_ADAPTIVE_GATE_ENABLED=1
T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX=5000
T1_LLM_ADAPTIVE_TOP_PCT_MAX=5.0
T1_LLM_ADAPTIVE_REJECT_ACTION=skip
T1_LLM_GATE_TOP_N=10
T1_LLM_FINAL_PICK_LIMIT=3
T1_LLM_CANDIDATE_PCT_CHANGE_MAX=3.5
T1_LLM_REQUIRE_REVIEW=1
T1_LLM_ALLOWED_ACTIONS=KEEP
THREE_DAY_ANALYSIS_LOOKBACK_DAYS=120
THREE_DAY_PRESELECT_LIMIT=120
THREE_DAY_FINAL_LIMIT=20
REALTIME_QUOTE_LIMIT=300
REALTIME_SIGNAL_LIMIT=80
REALTIME_DECISION_MODE=llm_review
REALTIME_AUTO_INTERVAL_SECONDS=60
REALTIME_LLM_CANDIDATE_LIMIT=12
CALL_AUCTION_ENABLED=1
CALL_AUCTION_START_TIME=09:15
CALL_AUCTION_DECISION_START_TIME=09:20
CALL_AUCTION_END_TIME=09:25
CALL_AUCTION_AUTO_INTERVAL_SECONDS=10
CALL_AUCTION_PRESELECT_LIMIT=120
CALL_AUCTION_LLM_REVIEW_LIMIT=20
CALL_AUCTION_FINAL_LIMIT=5
CALL_AUCTION_REQUIRE_LLM=1
CALL_AUCTION_LLM_TIMEOUT_SECONDS=30
CALL_AUCTION_MIN_PCT_CHANGE=0.0
CALL_AUCTION_MAX_PCT_CHANGE=6.8
CALL_AUCTION_MIN_VOLUME_RATIO=0.5
CALL_AUCTION_MIN_AMOUNT=3000000
CALL_AUCTION_MIN_FINAL_SCORE=55
SIMULATION_INITIAL_CASH=1000000
SIMULATION_ORDER_CASH_PCT=0.12
SIMULATION_MAX_POSITIONS=8
SIMULATION_FEE_RATE=0.0003
```

`.env` 已加入 `.gitignore`，生产环境建议使用环境变量或独立密钥管理，不要提交真实密码。

## 安装

后端依赖 Python 3.12；前端建议使用 Node.js 20+ 和 npm 10+。当前验证环境为 Node.js `v22.16.0`、npm `10.9.2`。

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .
```

## 建库建表

```powershell
astocks-collector init-db
```

会创建数据库 `astocks_collector` 和以下表：

- `stock_basic`：A股股票基础信息。
- `stock_daily`：A股日线行情，唯一键为 `(symbol, trade_date, adjust_type)`。
- `stock_analysis_pick`：T+1 大模型选股结果。
- `stock_call_auction_quote`：9:15-9:25 集合竞价融合行情快照，按交易日和股票保留最佳字段。
- `stock_call_auction_run`：集合竞价采集期和决策期运行记录。
- `stock_call_auction_pick`：集合竞价 LLM 复核通过候选，保存评分、理由、风险和因子快照。
- `stock_three_day_pick`：未来 3 个交易日涨势分析结果，保存量化因子快照、LLM 评分和降级状态。
- `stock_realtime_quote`：实时行情最新快照。
- `stock_realtime_signal`：实时分析信号历史。
- `stock_realtime_decision`：实时交易最终决策，记录规则动作、LLM 动作、最终动作、LLM 理由和降级状态。
- `simulation_account`：模拟交易账户。
- `simulation_position`：模拟交易持仓。
- `simulation_order`：模拟交易订单。
- `simulation_trade`：模拟交易成交明细。
- `collector_run`：采集任务运行记录。
- `collector_error`：采集失败明细。

## 同步股票基础信息

```powershell
astocks-collector sync-basic
```

## 历史回填

回填全部股票历史数据：

```powershell
astocks-collector backfill --start-date 19900101 --end-date 20260527
```

回填最近 3 年历史数据：

```powershell
astocks-collector backfill --years 3
```

只回填指定股票：

```powershell
astocks-collector backfill --symbols 000001,600000 --start-date 20200101 --end-date 20260527
```

小样本验证：

```powershell
astocks-collector backfill --symbols 000001 --start-date 20260520 --end-date 20260527
```

小样本验证最近 3 年：

```powershell
astocks-collector backfill --symbols 000001 --years 3
```

## 每日增量

采集最近 10 天窗口：

```powershell
astocks-collector incremental --days 10
```

指定股票增量：

```powershell
astocks-collector incremental --symbols 000001 --days 10
```

## 常驻定时调度

启动 APScheduler 常驻进程：

```powershell
astocks-collector scheduler
```

默认每天北京时间 `17:30` 执行最近窗口增量采集。该模式会一直占用当前终端，适合放在服务管理器、Windows 任务计划程序或进程守护工具中运行。

Windows 任务计划程序也可以直接每天触发一次增量命令：

```powershell
astocks-collector incremental --days 10
```

本项目提供了任务计划程序脚本：

```powershell
F:\astocks-collector\scripts\run_incremental.ps1
```

当前交付会创建名为 `AStocksCollectorIncremental` 的 Windows 每日任务，默认每天 `17:30` 执行该脚本。

## 查看状态

```powershell
astocks-collector status
```

该命令会输出核心表的行数。

## T+1 大模型选股分析

分析模块会读取 MySQL 中已有三年历史日线，先剔除 ST、*ST、退市、N/C 新股和异常行情，再计算趋势、动量、量能、波动、回撤、换手等技术因子，最后调用本地 OpenAI 兼容大模型复核候选池。

执行正式 T+1 分析：

```powershell
astocks-collector analyze-t1
```

`analyze-t1` 是一次性调试命令；需要让 T+1 模型长期运行时，使用长期任务入口：

```powershell
astocks-collector t1-loop
```

`t1-loop` 会一直占用当前终端并循环确保最新 T+1 模型结果可用，随后在开盘时执行实时模拟撮合；只有按 `Ctrl+C` 手动停止、或关闭承载它的后端进程时才会退出。可选参数包括 `--interval-seconds`、`--preselect-limit`、`--final-limit`、`--no-llm`、`--no-trade` 和 `--no-analysis`。

小范围验证：

```powershell
astocks-collector analyze-t1 --preselect-limit 20 --final-limit 5
```

不调用大模型，仅使用量化预筛：

```powershell
astocks-collector analyze-t1 --no-llm
```

分析结果会写入 `stock_analysis_pick`，字段包含排名、股票代码、股票名称、量化评分、大模型评分、综合评分、入选理由和风险。本次分析报告已写入 `reports/t1-picks-2026-05-27.md`。输出是基于历史行情和模型复核的短线候选，不保证 T+1 一定上涨，不构成投资建议。

## 未来 3 个交易日涨势分析

3 日分析模块会使用数据库中最新完整交易日作为基准，读取全 A 股近 `THREE_DAY_ANALYSIS_LOOKBACK_DAYS` 个交易日行情，剔除 ST、退市、N/C 新股和异常高波动标的，计算 3/5/10/20 日收益、均线排列、量能变化、换手率、波动率、回撤和突破强度等因子。程序先按 `three_day_score` 预筛，再交给 LLM 复核；LLM 超时、不可用或返回格式异常时，会保留量化排序并标记降级。

执行正式 3 日分析：

```powershell
astocks-collector analyze-3d --preselect-limit 120 --final-limit 20
```

不调用大模型，仅使用量化预筛：

```powershell
astocks-collector analyze-3d --no-llm --final-limit 20
```

分析结果会写入 `stock_three_day_pick`，字段包含分析日期、基准交易日、排名、股票代码、名称、量化评分、LLM 评分、综合评分、预期方向、理由、风险、因子快照、LLM 原始响应摘要和降级状态。该功能只用于策略研究和模拟观察，不接真实交易，不构成投资建议。

## 实时分析与模拟交易

实时分析会优先使用 AKShare 实时行情接口；当上游接口断连时，会自动降级使用数据库中最新完整交易日的 `stock_daily` 行情，并在快照来源中标记为降级数据。模拟交易只在本地数据库中撮合，不连接任何真实券商账户。

执行一次实时分析并模拟交易：

```powershell
astocks-collector realtime-once --limit 300
```

默认使用 `REALTIME_DECISION_MODE=llm_review`，即规则引擎先生成候选信号，再把高优先级候选交给大模型复核。大模型会返回 `BUY`、`SELL`、`WATCH` 或 `HOLD`、评分、理由和风险；如果大模型超时、不可用或返回格式异常，程序会保留规则动作并把本轮决策标记为降级。

只使用规则引擎决策：

```powershell
astocks-collector realtime-once --limit 300 --decision-mode rules
```

只生成实时信号，不执行模拟交易：

```powershell
astocks-collector realtime-once --limit 300 --no-trade
```

常驻循环执行，默认每 60 秒一轮：

```powershell
astocks-collector realtime-loop --limit 300 --interval-seconds 60
```

重置默认模拟账户：

```powershell
astocks-collector sim-reset --initial-cash 1000000
```

模拟交易规则：

- 初始资金由 `SIMULATION_INITIAL_CASH` 控制，默认 `1000000`。
- 单笔买入预算由 `SIMULATION_ORDER_CASH_PCT` 控制，默认使用可用现金的 `12%`。
- 最大持仓数量由 `SIMULATION_MAX_POSITIONS` 控制，默认 `8`。
- 手续费率由 `SIMULATION_FEE_RATE` 控制，默认 `0.0003`。
- 买入信号来自趋势、动量、流动性和风险评分；卖出信号来自分数转弱、盘中跌幅或跌破成本风控。
- `REALTIME_LLM_CANDIDATE_LIMIT` 控制每轮最多交给 LLM 复核的候选数量，默认 `12`。
- `REALTIME_AUTO_INTERVAL_SECONDS` 是前端自动刷新推荐间隔，默认 `60` 秒。

前端 `实时交易` 页面提供自动刷新、自动买卖、刷新间隔和决策模式控件，并在账户统计区展示总盈亏、持仓浮盈、已实现盈亏和收益率。实时分析和模拟买卖只在 A 股连续竞价时段生效，即工作日 `09:30-11:30`、`13:00-15:00`；非开盘时间后端会直接跳过 `/api/realtime/run`，不取行情、不写信号、不下单，前端会显示市场状态并禁用运行分析和自动买卖。自动刷新只在页面打开并开启开关时运行；开启自动买卖后，页面会按间隔触发 `/api/realtime/run`，否则只刷新看板数据。上一轮未完成时会跳过本轮，避免重复下单。

本功能用于策略验证与流程演示，不构成投资建议，也不代表真实交易可成交。

## 集合竞价 LLM 快速选股

集合竞价模块按北京时间工作日 `09:15:00-09:25:00` 分阶段运行：`09:15:00-09:20:00` 为采集期，只读取全 A 股实时行情并写入 `stock_call_auction_quote` 融合快照，不输出候选；`09:20:00-09:25:00` 为决策期，先写入本轮快照，再基于当日融合快照做规则预筛，并把 Top20 交给本地 OpenAI 兼容 LLM 复核。LLM 必须返回 `BUY_CANDIDATE` 或 `WATCH`；LLM 超时、不可用、返回格式异常或动作不合法时，本轮按量化分降级输出候选，并在运行摘要中标记 `llmFallback=true`。

执行一次集合竞价快选：

```powershell
astocks-collector call-auction-once
```

窗口内循环执行，默认每 10 秒一轮：

```powershell
astocks-collector call-auction-loop --interval-seconds 10
```

规则预筛默认剔除 ST、退市、N/C 新股、零价和异常行情，标准模式要求涨幅不低于 `0.0%`、综合分不低于 `55`，涨幅高于 `6.8%` 不再直接淘汰，而是按追高风险大幅扣分后交给 LLM；量比低于 `0.5` 或成交额低于 `300万` 也不直接淘汰，而是降低量能和风险评分后交给 LLM 判断，避免单次行情字段不完整或行情源仅返回涨幅榜时全市场无候选。若标准模式没有候选，会自动启用二次宽松预筛，把最低涨幅放宽到 `-0.3%`、综合分降到 `52`，高于 `7.5%` 的候选继续按追高风险扣分。结果写入 `stock_call_auction_run` 和 `stock_call_auction_pick`，只用于候选观察，不连接券商、不自动下单、不执行模拟买入。LLM 不可用时会按量化 Top 候选降级输出，页面会显示“规则降级”；9:20-9:25 期间申报后不可撤单，页面和命令输出都只代表候选建议，不构成投资建议。

## 前端数据 API

前端页面通过 FastAPI 读取 MySQL 中的真实采集结果，不直接连接数据库。

接口展示的行情数据使用 `ASTOCKS_ADJUST` 配置指定的复权口径，默认 `qfq` 前复权；切换为 `none` 或 `hfq` 后，列表、概览、分段统计和历史图表都会按对应口径查询。

全市场页面的“最新交易日”会优先选择覆盖股票数达到基础股票数 80% 以上的最近日期；如果库内只有小样本数据，则退回到最大交易日期。这样可以避免单只股票补采或临时数据把工作台误切到覆盖不完整的日期。

API 服务按只读方式运行，启动前请先执行 `astocks-collector init-db` 完成建库建表。

启动 API：

```powershell
astocks-collector serve-api --host 127.0.0.1 --port 8000
```

如果设置了 `ASTOCKS_WEB_DIST` 指向前端 `dist` 目录，FastAPI 会同时托管前端生产静态资源，适合 Docker 单容器部署。

主要接口：

- `GET /api/health`：健康检查。
- `GET /api/overview`：股票总数、最新交易日、日线行数和分析结果数量。
- `GET /api/stocks`：股票基础信息分页查询，支持关键词、交易所和风险筛选。
- `GET /api/segments`：交易所分布、ST/普通股分布、涨跌幅分段和成交额排行。
- `GET /api/history/{symbol}`：单只股票最近 N 条历史日线。
- `GET /api/analysis`：最新 T+1 大模型候选结果。
- `POST /api/t1-analysis/run`：手动触发一次 T+1 分析，主要用于调试或补跑。
- `GET /api/t1-trading`：T+1 专属账户、持仓、订单、候选和长期任务状态。
- `POST /api/t1-trading/run`：执行一次 T+1 模拟；`executeTrades=false` 时只预演不落交易。
- `POST /api/t1-trading/realtime-run`：读取真实实时行情并按 T+1 约束执行一次模拟撮合。
- `POST /api/t1-trading/reset`：重置 T+1 专属模拟账户。
- `GET /api/t1-trading/task`：查看 T+1 长期模型任务状态。
- `POST /api/t1-trading/task/start`：启动或更新 T+1 长期模型任务，手动停止前会持续运行。
- `POST /api/t1-trading/task/stop`：手动停止 T+1 长期模型任务。
- `GET /api/t1-quality`：查看最新 14:05 T+1 盘中质量选股运行记录和候选。
- `POST /api/t1-quality/run`：手动运行一次 14:05 质量选股；LLM 复核失败时记录 `SKIPPED` 且不自动买入。
- `GET /api/three-day-analysis`：最新未来 3 个交易日涨势候选结果。
- `POST /api/three-day-analysis/run`：手动触发一次 3 日分析，请求体支持 `preselect_limit`、`final_limit`、`trade_date` 和 `use_llm`。
- `GET /api/call-auction`：查看最新集合竞价 LLM 快速选股运行记录、候选和窗口状态。
- `POST /api/call-auction/run`：手动运行一次集合竞价快选，请求体支持 `finalLimit` 和 `force`。
- `GET /api/realtime`：实时分析、模拟账户、持仓和订单看板。
- `POST /api/realtime/run`：触发一次实时分析和模拟交易，请求体支持 `decision_mode=rules|llm_review` 和 `execute_trades=true|false`；仅在工作日 `09:30-11:30`、`13:00-15:00` 执行，非开盘时间返回 `skipped=true`。
- `POST /api/simulation/reset`：重置默认模拟账户。

## 前端页面

前端工程位于 `web` 目录，使用 Vite、React、TypeScript、Ant Design Pro Components 和 AntV G2Plot。

安装依赖：

```powershell
cd F:\astocks-collector\web
npm install
```

启动开发服务：

```powershell
npm run dev
```

浏览器访问：

```text
http://127.0.0.1:5173
```

构建生产资源：

```powershell
npm run build
```

本地预览生产资源：

```powershell
npm run preview
```

注意：`web/vite.config.ts` 中的 `/api` 代理只在 Vite 开发服务中生效。Docker 部署会让 FastAPI 直接托管 `dist` 静态资源；若使用 Nginx、IIS 或其他 Web 服务单独托管 `dist`，需要把 `/api` 反向代理到 `http://127.0.0.1:8000` 的 FastAPI 服务。

如果前端提示接口不可用，先检查 API 健康状态和 MySQL 连通性：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
Test-NetConnection 127.0.0.1 -Port 3306
```

当 MySQL 暂时不可达时，业务接口会返回 `503`，恢复数据库网络后刷新页面即可重新读取真实数据。
前端会在接口不可用时显示告警和空值，避免把数据库连接故障误展示为真实的 0 数据。

一键前端验收脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\astocks-collector\scripts\verify_frontend.ps1
```

该脚本会先执行后端编译和前端构建，再启动 API 与 Vite，随后根据 MySQL 是否可达自动选择真实数据验收或离线降级验收。截图会写入 `output/playwright`，日志会写入 `logs`，结束后会清理脚本启动的 API/Vite/Playwright 进程。
在线模式会校验 `/api/overview`、`/api/stocks`、`/api/segments`、`/api/history/000001`、`/api/analysis`、`/api/three-day-analysis` 均返回真实数据，并逐页截图概览、基础信息、分段数据、历史数据、分析数据、3日分析和实时交易；离线模式会校验这些业务接口返回 `503`，并确认前端菜单页面可访问且显示明确告警。

页面内容：

- `数据概览`：展示股票总数、最新日线覆盖、历史行情行数和 T+1 候选数量。
- `基础信息`：使用 ProTable 展示股票代码、名称、交易所、风险标签、最新收盘、涨跌幅、换手率和成交额。
- `分段数据`：使用 AntV 展示交易所分布、ST/普通股分布、涨跌幅分段和成交额 Top。
- `历史数据`：输入股票代码后展示近 N 日收盘价走势和成交量柱状图。
- `分析数据`：展示 `stock_analysis_pick` 中最新 T+1 候选的排名、评分、理由和风险。
- `3日分析`：展示 `stock_three_day_pick` 中最新未来 3 个交易日涨势候选，支持手动运行 3 日分析和刷新结果。
- `集合竞价`：展示 9:15-9:20 采集期、9:20-9:25 决策期、融合快照数量、LLM 复核/规则降级状态、候选排名、评分、理由和风险，窗口内默认 10 秒自动刷新。
- `实时交易`：展示实时分析信号、LLM 决策源、市场开盘状态、模拟账户资产、总盈亏、持仓浮盈、已实现盈亏、持仓和订单，并支持手动运行分析、自动刷新、自动模拟买卖和重置账户；运行分析和自动买卖只在开盘时段生效。
- `T+1交易`：展示 T+1 专属账户、持仓、候选、订单和长期模型任务状态，支持启动/停止长期任务、实时执行一次、预演、执行模拟和重置账户。
- `14:05质量选股`：14:05 T+1 盘中质量选股专属页，展示最新运行状态、行情统计、LLM 复核状态、手动运行/强制补跑按钮和质量候选明细。

## WSL Docker 部署

项目已支持在本机 WSL Docker 中部署为单个应用容器：FastAPI 提供 `/api` 数据接口，并托管前端生产页面；MySQL 仍使用已创建的 `astocks-mysql8` 容器。

部署脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\astocks-collector\scripts\deploy_wsl_docker.ps1
```

脚本会完成以下动作：

- 检查 `astocks-mysql8` MySQL 8 容器是否运行。
- 创建或复用 Docker 网络 `astocks-net`。
- 将 `astocks-mysql8` 接入 `astocks-net`。
- 使用 `docker compose up -d --build astocks-app` 构建并启动应用容器。

容器部署参数位于 `docker-compose.yml`，应用容器名为 `astocks-collector-app`，镜像名为 `astocks-collector-app:latest`，容器内通过 `MYSQL_HOST=astocks-mysql8` 访问数据库。

容器内默认通过 `DOCKER_LLM_BASE_URL=http://172.24.192.1:8317/v1` 访问 Windows 宿主机上的 CLI Proxy API。该地址来自 WSL 默认网关，当前环境已验证容器可以访问；如果 WSL 网络重启后网关变化，可用 `wsl -u root bash -lc "ip route | awk '/default/ {print $3}'"` 查到新网关，并更新 `.env` 中的 `DOCKER_LLM_BASE_URL`。如果大模型也部署在同一个 Docker 网络中，可把 `DOCKER_LLM_BASE_URL` 改成对应服务名，例如 `http://llm-service:8317/v1`。

3 日分析默认由 `THREE_DAY_ANALYSIS_LOOKBACK_DAYS=120`、`THREE_DAY_PRESELECT_LIMIT=120` 和 `THREE_DAY_FINAL_LIMIT=20` 控制。Docker 内同样支持这些环境变量，修改后重新构建或重启容器即可生效。

T+1 LLM 复核默认使用 `KEEP Top3` 平衡机会模式 + 保守锚定模式：`T1_LLM_ADAPTIVE_GATE_ENABLED=1` 表示先判断是否值得启用 LLM，只有当全市场量化候选数量不超过 `T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX=5000` 且量化 Top10 当日平均涨幅不超过 `T1_LLM_ADAPTIVE_TOP_PCT_MAX=5.0` 时才调用 LLM 复核；`T1_LLM_ADAPTIVE_REJECT_ACTION=skip` 表示门控不过时直接空仓。触发 LLM 后，`T1_LLM_FINAL_PICK_LIMIT=3` 只保留前 3 只，`T1_LLM_CANDIDATE_PCT_CHANGE_MAX=3.5` 剔除当日涨幅过高的追高候选，`T1_LLM_GATE_TOP_N=10` 固定用量化 Top10 热度做市场门控，`T1_LLM_REQUIRE_REVIEW=1` 表示没有 LLM 复核或复核异常时直接跳过，`T1_LLM_ALLOWED_ACTIONS=KEEP` 表示只允许 LLM 评为可保留的候选进入最终名单，历史上容易放大回撤的 `BOOST` 只保留为审计动作，不作为默认买入依据。若需要恢复低频胜率优先模式，可使用 `T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX=2500`、`T1_LLM_ADAPTIVE_TOP_PCT_MAX=4.0`、`T1_LLM_FINAL_PICK_LIMIT=10`、`T1_LLM_CANDIDATE_PCT_CHANGE_MAX=0`；若需要恢复高参与度收益优先模式，可把 `T1_LLM_ADAPTIVE_REJECT_ACTION` 改为 `quant`，并按历史收益模式使用 `T1_LLM_ADAPTIVE_CANDIDATE_COUNT_MAX=4080`、`T1_LLM_ADAPTIVE_TOP_PCT_MAX=3.25`。触发 LLM 时，`T1_LLM_REVIEW_POOL_MULTIPLIER=2` 表示只审量化前排，`T1_LLM_SCORE_WEIGHT=0.20`、`T1_LLM_MAX_ADJUSTMENT=0.8` 控制 LLM 加减分权重和单票最大扰动，`T1_LLM_RANK_PENALTY=0.02` 防止低排名候选大幅跃迁，`T1_LLM_AVOID_PENALTY=2.0` 用于明显风险候选降权，`T1_LLM_PROTECTED_TOP_N=0` 表示允许 LLM 对量化前排做有限复核。该模式仍保留量化排序为主，只让 LLM 做风险复核和小幅校准，不构成投资建议。

访问地址：

```text
http://127.0.0.1:8003
```

默认对外端口为 `8003`，容器内 FastAPI 仍监听 `8000`。如果要使用其他端口，可执行 `scripts\deploy_wsl_docker.ps1 -Port 8000` 或在 WSL 中设置 `ASTOCKS_APP_PORT=8000` 后执行 `docker compose up -d --build astocks-app`。

部署后可用以下命令查看状态和日志：

```powershell
wsl -u root bash -lc "cd /mnt/f/astocks-collector && docker compose ps"
wsl -u root bash -lc "docker logs -f astocks-collector-app"
```

接口冒烟验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8003/api/health
Invoke-RestMethod http://127.0.0.1:8003/api/overview
```

重新构建部署：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\astocks-collector\scripts\deploy_wsl_docker.ps1
```

停止应用容器：

```powershell
wsl -u root bash -lc "cd /mnt/f/astocks-collector && docker compose stop astocks-app"
```

上述停止命令只停止应用容器，不会停止外部 MySQL 容器 `astocks-mysql8`。

## 表结构要点

`stock_daily` 核心字段：

- `symbol`：六位股票代码。
- `trade_date`：交易日期。
- `adjust_type`：复权类型，空字符串表示不复权，`qfq` 表示前复权，`hfq` 表示后复权。
- `open_price`、`close_price`、`high_price`、`low_price`：价格字段。
- `pre_close`：根据收盘价和涨跌额估算的昨收价。
- `volume`：成交量，单位为手。
- `amount`：成交额，单位为元。
- `pct_change`、`change_amount`、`amplitude`、`turnover_rate`：行情指标。

## 运维建议

- 全市场历史回填数据量较大，建议首次运行时先用 `--symbols` 或 `--limit` 小范围验证。
- 当前项目支持 `--years 3` 直接回填最近 3 年历史数据。
- 历史回填默认使用 `ASTOCKS_MAX_WORKERS=2` 进程池并发采集，可按数据源稳定性调低或调高。
- AKShare 上游数据源可能限制高频请求，当前默认每只股票间隔 `0.35` 秒，可按网络情况调大。
- 每日增量建议安排在 A股收盘后，例如北京时间 `17:30`。
- T+1 分析依赖收盘后完整日线数据，建议每日增量采集完成后再执行。
- 重复运行同一日期范围是安全的，程序会按唯一键更新已有行。
- 如果全市场回填中途失败，重新执行同一命令即可继续补齐。

## 当前交付记录

- `2026-05-27`：创建 Python 采集器工程，完成建表、股票基础信息同步、历史回填、每日增量、定时调度和 README 文档。
- `2026-05-28`：新增 ST 剔除、技术因子预筛、本地 `gpt-5.5` 大模型复核和 T+1 候选落库。
- `2026-05-28`：新增 FastAPI 前端数据接口和 Ant Design Pro + AntV 数据工作台，覆盖基础信息、分段数据、历史数据和分析数据页面。
- `2026-05-28`：优化前端/API 降级行为，API 按只读方式启动，数据库不可达时返回 `503`，前端显示明确告警而非假 0 数据。
- `2026-05-28`：新增 `scripts\verify_frontend.ps1` 与 `web\scripts\verify_frontend.mjs`，支持数据库恢复后的真实数据验收和数据库不可达时的离线降级验收。
- `2026-05-28`：将数据库切换为本机 WSL Docker MySQL 8，连接地址 `127.0.0.1:3306`，root 密码 `root`。
- `2026-05-28`：完成前端在线验收，本机库包含真实 A股基础信息、历史行情和 `stock_analysis_pick=10`，五个前端页面和 AntV 图表均通过 Playwright 验证。
- `2026-05-28`：已启动全市场近 3 年历史行情后台回填，进程号记录在 `logs\backfill-3y.pid`，当前日志路径记录在 `logs\backfill-3y.current.log`。
- `2026-05-28`：新增并完成 WSL Docker 应用部署，容器 `astocks-collector-app` 通过 `http://127.0.0.1:8003` 提供前端页面和 `/api` 接口，连接外部 MySQL 容器 `astocks-mysql8`，并通过容器页面 Playwright 在线验收。
- `2026-05-28`：全市场近 3 年历史行情回填主任务写入 `3,858,820` 行，单只失败股票 `689009` 已用自动数据源补采成功并写入 `726` 行；同时修复单只补采日期影响全市场最新交易日展示的问题。
- `2026-05-28`：新增实时分析与模拟交易功能，完成实时/降级行情快照、实时信号、模拟账户、持仓、订单、成交表结构和前端 `实时交易` 页面；当前验证生成 `300` 条行情快照、`80` 条最新信号并执行 `8` 笔模拟买入订单。
- `2026-05-28`：实时交易页面新增自动刷新、自动买卖、决策模式和 LLM 状态展示；后端新增 `stock_realtime_decision`，支持规则预筛 + LLM 复核，LLM 异常时自动降级规则引擎。
- `2026-05-28`：新增全 A 股未来 3 个交易日涨势分析，完成 `stock_three_day_pick`、`analyze-3d`、`/api/three-day-analysis` 和前端 `3日分析` 页面，支持量化预筛 + LLM 复核与量化降级。
- `2026-05-29`：实时交易页面账户统计区新增总盈亏、持仓浮盈和已实现盈亏金额，便于直接查看模拟交易收益拆分。
- `2026-05-29`：修复 WSL Docker 容器访问 Windows 本机 LLM 服务的地址，将 `DOCKER_LLM_BASE_URL` 指向 WSL 默认网关 `172.24.192.1:8317`，避免 `host.docker.internal` 被解析到 Docker 网桥导致连接拒绝。
- `2026-05-29`：实时交易引擎新增 A 股开盘时段守卫，非工作日或非 `09:30-11:30`、`13:00-15:00` 时跳过实时分析和模拟交易，前端同步显示市场状态并禁用相关操作。
- `2026-05-31`：完成 T+1 历史收益验证；使用数据库 `stock_daily` 最近 60 个完整信号日、600 笔 Top10 等权样本，严格按 D 日收盘出信号、D+1 开盘买入、D+2 开盘卖出并扣双边手续费，交易胜率 `49.50%`，日组合盈利率 `65.00%`，日组合累计收益 `43.28%`，同期全市场等权基准累计收益 `-8.04%`。
- `2026-05-31`：新增 `scripts/t1_llm_backtest.py`，完成同口径 T+1 量化 Top80 + LLM 复核 Top10 历史验证；60 个信号日内 LLM 无降级，平均 Top10 重合 `3.33` 只，交易胜率 `46.50%`，日组合累计收益 `9.04%`，低于纯量化 `43.28%`，完整结果见 `reports/t1_llm_backtest_60_20260531.json`。
- `2026-05-31`：优化 T+1 LLM 复核为保守锚定模式，LLM 仅审量化 Top30 并做小幅加减分，低排名候选不能大幅跃迁；同一 60 日样本回测中，平均 Top10 重合提升至 `8.30` 只，日组合累计收益提升至 `48.68%`，最大回撤改善至 `-5.72%`，完整结果见 `reports/t1_llm_backtest_conservative_60_20260531.json`。
- `2026-05-31`：再次优化 T+1 LLM 复核参数，基于已缓存 LLM 判断完成 `22,500` 组一致性网格搜索，并按生产排序复验；最优默认参数调整为 Top20 复核、LLM 权重 `0.20`、最大扰动 `0.8`、排名惩罚 `0.02`、风险降权 `2.0`、前排保护 `0`。同一 60 日样本中单笔胜率 `50.00%`、日组合累计收益 `48.78%`、最大回撤 `-6.70%`，完整结果见 `reports/t1_llm_backtest_optimized_60_20260531.json` 和 `reports/t1_llm_backtest_optimized_grid_60_20260531.json`。
- `2026-05-31`：完成最近 `120` 个完整信号日 T+1 历史模拟，验证区间为 `2025-11-24` 至 `2026-05-26`，严格按 D 日收盘出信号、D+1 开盘买入、D+2 开盘卖出并扣双边手续费；纯量化单笔胜率 `46.29%`、日组合累计收益 `46.86%`、最大回撤 `-8.61%`，当前 LLM 复核版单笔胜率 `46.04%`、日组合累计收益 `45.46%`、最大回撤 `-10.81%`，120 日窗口内 LLM 复核未优于纯量化。完整结果见 `reports/t1_quant_backtest_120_20260531.json` 和 `reports/t1_llm_backtest_120_20260531.json`。
- `2026-05-31`：使用 LLM 审阅 120 日归因搜索结果后，新增 T+1 LLM 自适应门控：当全市场量化候选数量 `<=4080` 且量化 Top10 当日平均涨幅 `<=3.25%` 时启用 LLM，否则回退纯量化。复验同一 120 日窗口，自适应混合策略单笔胜率 `46.46%`、日组合胜率 `58.33%`、日组合累计收益 `61.56%`、最大回撤 `-8.10%`，相比纯量化累计收益提升 `14.70` 个百分点，相比全程 LLM 复核提升 `16.10` 个百分点；完整结果见 `reports/t1_llm_adaptive_backtest_120_20260531.json`。
- `2026-05-31`：随机抽取 `7` 个完整信号日复核 T+1 策略，随机种子 `1780226981`，抽样日期为 `2025-11-27`、`2025-12-10`、`2026-01-26`、`2026-02-13`、`2026-03-03`、`2026-03-06`、`2026-05-06`；纯量化累计收益 `3.91%`，全程 LLM 复核累计收益 `4.49%`，自适应 LLM 门控累计收益 `6.37%`、日组合胜率 `85.71%`，完整结果见 `reports/t1_random7_backtest_20260531_seed1780226981.json`。
- `2026-05-31`：按总资产 `10000` 元随机抽取连续 `5` 个交易日做 T+1 模拟实战，随机种子 `1780227328`，信号日为 `2025-12-22` 至 `2025-12-26`；每日最多 `10` 只等权、按整股买入、剩余现金留存、D+1 开盘买入、D+2 开盘卖出并复投。最终资产 `9985.45` 元，累计盈亏 `-14.55` 元，累计收益 `-0.15%`，最大回撤 `-1.43%`，完整结果见 `reports/t1_sim5_10000_20260531_seed1780227328.json`。
- `2026-05-31`：沿用同一组连续 `5` 个信号日和 `10000` 元本金，强制全程使用 LLM 复核结果做 T+1 模拟；其中 `2025-12-24` 因历史 LLM 缓存缺失回退量化。最终资产 `9906.75` 元，累计盈亏 `-93.25` 元，累计收益 `-0.93%`，最大回撤 `-1.82%`，弱于自适应门控版本的 `9985.45` 元，完整结果见 `reports/t1_sim5_10000_forced_llm_20260531.json`。
- `2026-05-31`：根据连续 5 日和 120 日复验结果，将默认 T+1 策略切换为胜率优先门控：当全市场候选数量 `<=2500` 且量化 Top10 平均涨幅 `<=4.0%` 时才启用 LLM，否则空仓。最近 120 个完整信号日中实际交易 `32` 天，交易日胜率 `59.38%`、单笔胜率 `50.47%`、累计收益 `18.26%`、最大回撤 `-4.54%`；相比纯量化交易日胜率提升 `2.71` 个百分点、最大回撤改善 `4.06` 个百分点，但累计收益低于收益优先模式。完整结果见 `reports/t1_winrate2500_backtest_120_20260531.json` 和 `reports/t1_winrate_strategy_search_20260531.json`；同一组 `10000` 元连续 5 日样本全部触发空仓，最终资产 `10000.00` 元，完整结果见 `reports/t1_sim5_10000_winrate_20260531.json`。
- `2026-05-31`：使用当前胜率优先 T+1 策略测试最近 `10` 个完整信号日，区间为 `2026-05-13` 至 `2026-05-26`，本金 `10000` 元；前 `6` 天因候选池超过 `2500` 空仓，后 `4` 天触发 LLM 门控交易。最终资产 `10534.42` 元，累计盈利 `534.42` 元，收益率 `5.34%`，最大回撤 `-0.66%`；实际交易日胜率 `75.00%`，单笔胜率 `47.50%`，完整结果见 `reports/t1_sim10_10000_winrate_20260531.json`。
- `2026-05-31`：新增 `scripts/t1_continuous5_optimizer.py`，用于搜索连续 5 日资金曲线和 LLM 辅助策略；默认策略切换为连续 5 日机会模式：候选数 `<=4500`、Top10 平均涨幅 `<=4.0%` 时启用 LLM，只保留当日涨幅 `<=3.5%` 的前 `5` 只。此前 `2025-12-22` 至 `2025-12-26` 连续 5 日样本从空仓 `0.00%` 提升到 `+0.91%`，最终资产 `10091.02` 元；最近 10 日样本最终资产 `10425.77` 元，收益 `+4.26%`；120 日回测中 LLM 机会模式累计收益 `97.29%`、单笔胜率 `47.95%`、最大回撤 `-10.03%`。完整结果见 `reports/t1_continuous5_optimizer_20260531.json`、`reports/t1_sim5_10000_opportunity_20260531.json`、`reports/t1_sim10_10000_opportunity_20260531.json` 和 `reports/t1_opportunity_backtest_120_20260531.json`。
- `2026-05-31`：按当前连续 5 日机会模式随机抽取一组连续 `5` 个完整信号日，随机种子 `1780235647`，信号日为 `2026-04-17`、`2026-04-20`、`2026-04-21`、`2026-04-22`、`2026-04-23`，本金 `10000` 元。最终资产 `10336.83` 元，累计盈利 `336.83` 元，收益率 `3.37%`，最大回撤 `-0.20%`；实际交易 `4` 天、空仓 `1` 天，实际交易日胜率 `75.00%`，单笔胜率 `55.00%`。完整结果见 `reports/t1_random5_10000_opportunity_1780235647.json`。
- `2026-05-31`：根据上述随机 5 日、最近 10 日和 120 日模拟结果继续优化 T+1 LLM 策略，默认切换为平衡机会模式：候选数 `<=5000`、Top10 平均涨幅 `<=5.0%`、严格要求 LLM 复核、仅允许 `BOOST/KEEP` 动作、最终保留涨幅 `<=3.5%` 的前 `5` 只。新版 120 日官方回测区间为 `2025-11-24` 至 `2026-05-26`，实际交易 `109` 天，日组合累计收益 `95.13%`、单笔胜率 `49.07%`、最大回撤 `-11.67%`，相比纯量化累计收益提升 `48.27` 个百分点；最近 10 日本金 `10000` 元复验最终资产 `10425.77` 元、收益 `4.26%`；同一组随机连续 5 日样本最终资产提升到 `10645.23` 元、收益 `6.45%`、交易日胜率 `75.00%`、单笔胜率 `70.00%`。完整结果见 `reports/t1_balanced_backtest_120_20260531.json`、`reports/t1_balanced_backtest_10_20260531.json` 和 `reports/t1_strict_random5_optimizer_1780235647.json`。
- `2026-05-31`：使用优化后的平衡机会模式再次随机抽取连续 `5` 个完整信号日测试，随机种子 `1780238760`，信号日为 `2026-01-05` 至 `2026-01-09`，本金 `10000` 元；其中 `2026-01-06` 因历史 LLM 缓存缺失按严格复核规则空仓，其余 `4` 个实际交易日全部盈利。最终资产 `10788.39` 元，累计盈利 `788.39` 元，收益率 `7.88%`，最大回撤 `0.00%`，实际交易日胜率 `100.00%`，单笔胜率 `65.00%`。本次同时修复 `scripts/t1_continuous5_optimizer.py` 对新版 `T1_LLM_ALLOWED_ACTIONS` 参数的兼容问题，完整结果见 `reports/t1_random5_optimized_1780238760.json`。
- `2026-05-31`：继续使用优化后的平衡机会模式随机抽取连续 `20` 个完整信号日测试，随机种子 `1780239851`，信号日为 `2025-12-31` 至 `2026-01-29`，本金 `10000` 元；实际交易 `15` 天，`5` 天因历史 LLM 缓存缺失按严格复核规则空仓。最终资产 `10661.88` 元，累计盈利 `661.88` 元，收益率 `6.62%`，最大回撤 `-5.52%`，实际交易日胜率 `60.00%`，单笔胜率 `45.33%`。样本前半段收益较强，后半段在 `2026-01-14`、`2026-01-15`、`2026-01-28`、`2026-01-29` 出现回撤，说明 20 日窗口下仍需要继续滚动验证回撤控制。完整结果见 `reports/t1_random20_optimized_1780239851.json`。
- `2026-05-31`：依据随机 `5` 日和随机 `20` 日结果再次优化 LLM 决策：默认从 `BOOST/KEEP Top5` 改为 `KEEP Top3`，即 LLM 只把稳态可保留的候选放入交易名单，`BOOST` 不再作为默认买入依据，同时提示词要求模型对单日脉冲、放量过猛和回撤恶化更加保守。官方 120 日复验区间仍为 `2025-11-24` 至 `2026-05-26`，实际交易 `109` 天，累计收益 `94.78%`、单笔胜率 `49.39%`、最大回撤 `-12.69%`；同一随机 `20` 日窗口最终资产从 `10661.88` 元提升到 `10955.23` 元，收益率从 `6.62%` 提升到 `9.55%`，实际交易日胜率从 `60.00%` 提升到 `66.67%`，单笔胜率从 `45.33%` 提升到 `48.89%`。代价是该 20 日窗口最大回撤从 `-5.52%` 扩大到 `-6.36%`，后续如优先控制回撤可增加连续亏损冷却规则。完整结果见 `reports/t1_keep_top3_backtest_120_20260531.json` 和 `reports/t1_random20_keep_top3_1780239851.json`。
- `2026-05-31`：使用当前 `KEEP Top3` 策略再次随机抽取连续 `20` 个完整信号日测试，随机种子 `1780241723`，信号日为 `2025-12-22` 至 `2026-01-20`，本金 `10000` 元；实际交易 `15` 天，`5` 天因历史 LLM 缓存缺失按严格复核规则空仓。最终资产 `12274.22` 元，累计盈利 `2274.22` 元，收益率 `22.74%`，最大回撤 `-2.30%`，实际交易日胜率 `73.33%`，单笔胜率 `53.33%`。完整结果见 `reports/t1_random20_keep_top3_1780241723.json`。
- `2026-05-31`：使用当前 `KEEP Top3` 策略随机抽取连续 `40` 个完整信号日测试，随机种子 `1780242095`，信号日为 `2026-02-06` 至 `2026-04-13`，本金 `10000` 元；该窗口 `40` 天全部有历史 LLM 缓存，因此没有空仓。最终资产 `11308.13` 元，累计盈利 `1308.13` 元，收益率 `13.08%`，最大回撤 `-11.00%`，实际交易日胜率 `40.00%`，单笔胜率 `47.50%`。该窗口收益主要来自 `2026-03-26`、`2026-03-31`、`2026-03-20` 等强盈利日，同时 `2026-03-02`、`2026-03-27`、`2026-04-02` 出现较大回撤，说明拉长到 40 日后仍需要关注连续亏损冷却和单日风险限制。完整结果见 `reports/t1_random40_keep_top3_1780242095.json`。
- `2026-05-31`：继续按当前 `KEEP Top3` 策略完成连续 `80` 日和 `120` 日窗口压力测试，并将 `40/80/120` 三组结果合并评分。随机 `80` 日窗口随机种子 `1780242546`，信号日为 `2025-12-08` 起连续 `80` 个完整信号日，最终资产 `14210.49` 元，累计收益 `42.10%`，最大回撤 `-12.45%`，实际交易日胜率 `50.00%`，单笔胜率 `48.61%`；`120` 日窗口为当前 LLM 缓存覆盖的完整区间 `2025-11-24` 至 `2026-05-26`，最终资产 `19494.83` 元，累计收益 `94.95%`，最大回撤 `-12.58%`，实际交易日胜率 `52.29%`，单笔胜率 `49.24%`。跨窗口评分中，当前默认 `候选数<=5000 + Top10平均涨幅<=5.0% + 最新涨幅<=3.5% + 严格LLM复核 + 只允许KEEP + Top3` 在严格 LLM 门控策略中排名第一，因此本轮优化结论为保持当前生产默认策略，不再放宽到 `Top5` 或重新启用 `BOOST` 买入。完整结果见 `reports/t1_random80_keep_top3_1780242546.json`、`reports/t1_random120_keep_top3_full.json` 和 `reports/t1_multi_window_strategy_20260531.json`。
- `2026-06-01`：使用 `gsap` 与 `@gsap/react` 优化前端页面动效，入口统一注册 `useGSAP`，页面切换增加轻量入场动画，T+1 实时模拟工作台增加命令区、指标卡、任务区和数据区的分层入场与指标刷新反馈，并尊重系统 `prefers-reduced-motion` 设置。同步修复 T+1 总资产输入框的 Ant Design 弃用写法，更新 Playwright 验收脚本中的 T+1 页面识别文案；已重新构建 Docker 应用并通过 `http://127.0.0.1:8003` 在线验收，前端页面、图表、实时交易和 T+1 页面均无控制台错误。
- `2026-06-01`：将 T+1 执行升级为长期模型任务：前端 `启动T+1模型` 和命令行 `astocks-collector t1-loop` 都会持续运行，循环中先确保最新交易日的 T+1 模型结果已生成，再在开盘时执行实时模拟；任务状态写入失败不会中断循环，页面会在任务运行中自动刷新，只有手动停止或后端进程退出才会结束。
- `2026-06-01`：修复 T+1 长期任务执行后股价和盈利不实时变化的问题；根因是看板刷新时用日线收盘价覆盖了实时任务写入的持仓价和浮盈，且容器内 AKShare 全市场实时接口异常时没有定向行情兜底。现在开盘时 `/api/t1-trading` 会按持仓和候选股定向读取新浪实时行情，刷新持仓最新价、市值、浮动盈亏、总资产和候选交易价；实时行情为空时本轮实时任务跳过，缺少单只 quote 时只刷新可卖数量并保留最近估值，前端在开盘或任务运行时自动轮询刷新。
- `2026-06-02`：执行当日 T+1 候选生成任务。先用 `incremental --days 10 --end-date 20260602` 补齐最新日线，盘中日线接口未返回 `2026-06-02`，最新完整交易日为 `2026-06-01`；随后运行 `analyze-t1 --trade-date 20260601 --preselect-limit 120 --final-limit 20`，因 LLM 返回 JSON 格式异常被严格模式跳过，改用 `--no-llm` 生成量化候选并落库，最终候选为 `603439 三力制药`、`600888 新疆众和`、`000151 中成股份`。
- `2026-06-02`：为 14:05 T+1 盘中质量选股新增左侧菜单 `14:05质量选股` 和独立路由 `/t1-quality`，页面集中展示运行状态、行情统计、LLM 复核状态、手动运行/强制补跑和质量候选明细；原 `T+1交易` 工作台继续保留质量选股摘要。
- `2026-06-02`：根据随机 20 次 14:05 质量选股近似回测结果优化盘中质量策略。新版默认接入信号日前最近完整日线计算 `ma5/ma20/ret5/ret20`，要求短中期趋势向上、量比不低于 `1.2`、振幅不高于 `8.0%`，并将涨幅上限从 `7.5%` 收紧到 `5.8%`、质量分门槛从 `64` 提高到 `66`。同一批随机 20 日复验中，旧默认单票准确率 `48.75%`、平均次日收益 `0.3512%`，新版单票准确率提升到 `50.00%`、平均次日收益提升到 `0.5099%`，组合正收益天数从 `12` 天提升到 `16` 天。
- `2026-06-02`：继续按“目标 80% 准确率”优化 14:05 质量选股为高置信低频模式。默认最终买入数从 `3` 降至 `2`，新增候选池区间门控 `20-300`、前排 Top10 平均涨幅上限 `3.5%`、全市场平均涨幅优势区间 `0.5%-2.0%`，并增加 20 日波动率 `<=4.5` 和最大回撤 `<=12.0%` 过滤。最近 60 个可验证信号日复验区间 `2026-03-03` 至 `2026-05-29`，实际交易 `8` 天、跳过 `52` 天、验证 `16` 票、命中 `13` 票，单票准确率 `81.25%`、交易日胜率 `87.50%`、平均次日收益 `1.3110%`。同一组历史随机 20 日独立复验仅触发 `1` 个交易日、`2` 票，准确率 `50.00%`，说明当前 80% 是低频高置信目标模式的近期窗口结果，不应理解为跨市场稳定胜率承诺。
## T+1 专属模拟交易与分析

- 新增左侧菜单 `T+1交易`，也可直接访问 `/t1-trading`，用于 A 股 T+1 规则下的模拟交易和 T+1 候选分析观察；新增 `14:05质量选股` 专属页，也可直接访问 `/t1-quality`，用于集中观察盘中质量选股链路。
- 项目整体 UI 已统一为金融行业工作台风格：深色交易侧栏、中性低噪声背景、紧凑表格、红绿盈亏色、等宽数字和更清晰的卡片分层。
- 后端接口：
  - `POST /api/t1-analysis/run`：手动运行一次 T+1 分析，主要用于调试或补跑，结果继续写入现有 T+1 分析结果表。
  - `GET /api/t1-trading`：返回 T+1 账户、持仓、订单、候选分析结果。
  - `POST /api/t1-trading/run`：执行一次 T+1 模拟；`executeTrades=false` 时只预演不落交易。
  - `POST /api/t1-trading/realtime-run`：读取真实实时行情并按 T+1 约束执行一次模拟撮合。
  - `POST /api/t1-trading/reset`：重置 T+1 专属模拟账户。
  - `GET /api/t1-trading/task`：查看 T+1 长期模型任务状态。
  - `POST /api/t1-trading/task/start`：启动或更新 T+1 长期模型任务，循环确保最新 T+1 模型结果可用，并在开盘时自动读取实时行情并模拟交易。
  - `POST /api/t1-trading/task/stop`：手动停止 T+1 长期模型任务。
  - `GET /api/t1-quality`：查看最新 14:05 T+1 盘中质量选股运行记录和候选。
  - `POST /api/t1-quality/run`：手动运行一次 14:05 T+1 质量选股，支持 `executeTrades`、`finalLimit` 和 `force`。
- 数据库使用独立 `t1_simulation_account`、`t1_simulation_position`、`t1_simulation_order`、`t1_simulation_trade` 表，不影响原实时模拟交易表。
- T+1 约束：买入交易日 `available_quantity=0`，下一交易日起才允许卖出；当前卖出规则为可卖持仓触发止盈/止损后模拟成交。
- T+1 盘中质量选股会在交易日 `14:05` 后自动触发；`scheduler` 进程和 `serve-api` 进程内调度器都会调用同一套 `t1_quality` 服务，数据库运行记录用于避免当天重复自动买入。该策略使用实时行情、上一交易日前完整日线趋势因子、盘中量能风险过滤、20 日波动/回撤过滤和高置信市场门控生成候选；门控未通过或 LLM 超时、不可用、未保留候选时写入 `SKIPPED`，不会自动买入。复核成功后默认最多买入 `2` 只，并写入独立 `stock_t1_intraday_run`、`stock_t1_intraday_pick` 表，复用 T+1 专属账户自动模拟买入。
- 页面已重构为 T+1 实时模拟工作台：顶部集中展示市场状态、任务状态、分析日和下次到期时间；中部展示账户核心指标、交易操作和到期任务管理；底部通过标签页切换持仓、候选和订单。
- 页面展示 T+1 分析候选、T+1 锁定持仓、可卖日期、具体盈亏金额和模拟订单，并支持在页面中输入初始总资产后重置 T+1 模拟账户。
- T+1 长期模型任务运行在后端 FastAPI 进程内，可在页面中启动、停止和查看下次循环时间；任务手动停止前会持续运行。开盘时，T+1 看板会按持仓和候选股定向读取新浪实时行情，刷新持仓最新价、市值、浮动盈亏、总资产和候选交易价；实时行情暂缺或个别持仓缺少 quote 时，页面保留最近一次估值，只刷新 T+1 可卖数量，避免把实时估值覆盖回日线收盘价。任务只做模拟交易，不连接真实券商，也不构成投资建议。Docker 部署时该任务随 `astocks-collector-app` 应用容器生命周期存在，停止应用容器会结束任务，但不会停止外部 MySQL。
- 最近一次 T+1 历史验证口径：日线数据范围 `2023-05-29` 至 `2026-05-28`，验证信号日 `2026-02-26` 至 `2026-05-26`，最后一组交易为 `2026-05-27` 买入、`2026-05-28` 卖出；验证未调用 LLM，仅复刻当前量化规则，且不包含真实盘口滑点、涨跌停排队和券商撮合约束。
- 最近一次 T+1 LLM 复核验证使用同一批 60 个信号日：旧版量化 Top80 交给 `gpt-5.5` 自由复核生成 Top10 时，日组合累计收益只有 `9.04%`、最大回撤 `-10.69%`；第一版保守锚定模式只审量化 Top30 并小幅调整后，日组合累计收益 `48.68%`、最大回撤 `-5.72%`；Top20 保守复核参数在 60 日窗口内单笔胜率 `50.00%`、日组合累计收益 `48.78%`、最大回撤 `-6.70%`。扩展到最近 120 个完整信号日后，纯量化累计收益 `46.86%`，全程 LLM 复核累计收益 `45.46%`；收益优先自适应混合策略累计收益 `61.56%`、最大回撤 `-8.10%`；低频胜率优先门控累计收益 `18.26%`、最大回撤 `-4.54%`；连续 5 日机会模式累计收益 `97.29%`、单笔胜率 `47.95%`、最大回撤 `-10.03%`；`BOOST/KEEP Top5` 平衡机会模式 120 日累计收益 `95.13%`、单笔胜率 `49.07%`、最大回撤 `-11.67%`。当前默认进一步改为 `KEEP Top3`，严格要求 LLM 复核且只允许 `KEEP` 候选，120 日累计收益 `94.78%`、单笔胜率 `49.39%`、最大回撤 `-12.69%`；该模式牺牲少量 120 日累计收益和回撤，换取更高单笔胜率，并改善最近随机 20 日窗口收益。
