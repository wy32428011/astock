# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

`astocks-collector` is an A-share market data collection, analysis, simulation trading, and web dashboard project.

- Backend: Python package `astocks_collector` under `src/astocks_collector`.
- CLI entry point: `astocks-collector = "astocks_collector.cli:main"` in `pyproject.toml`.
- API: FastAPI app created in `src/astocks_collector/web_api.py`.
- Frontend: React + TypeScript + Vite app under `web`, calling backend routes through `/api`.
- Database: MySQL; main schema lives in `db.py`, with additional T+1-specific tables created by T+1 modules.

The README is already detailed. Keep this file as a concise command and architecture index instead of duplicating README sections.

## Common Commands

### Backend setup and CLI

```powershell
python -m pip install -r requirements.txt
python -m pip install -e .

astocks-collector init-db
astocks-collector sync-basic
astocks-collector incremental --days 10
astocks-collector status
```

Run the FastAPI backend locally:

```powershell
astocks-collector serve-api --host 127.0.0.1 --port 8000
```

Other common analysis / trading commands:

```powershell
astocks-collector analyze-t1
astocks-collector t1-loop
astocks-collector analyze-3d --preselect-limit 120 --final-limit 20
astocks-collector realtime-once --limit 300
astocks-collector realtime-loop --limit 300 --interval-seconds 60
astocks-collector sim-reset --initial-cash 1000000
```

### Frontend setup and build

```powershell
cd web
npm install
npm run dev
npm run build
npm run preview
```

`npm run dev` starts Vite on `127.0.0.1:5173`. `web/vite.config.ts` proxies `/api` to `http://127.0.0.1:8000`, so the backend API should be running during frontend development.

### Validation and tests

Backend compile check used by the frontend verification script:

```powershell
python -m compileall src
```

Full frontend / API verification script:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_frontend.ps1
```

If the current environment has `pytest` installed, run Python tests with:

```powershell
python -m pytest
python -m pytest tests\test_t1_trading.py::test_next_trading_date_after_uses_strictly_later_date -v
```

If the current environment has `ruff` installed, lint with:

```powershell
python -m ruff check src tests
```

Note: `pytest` and `ruff` are useful for this repository, but they are not explicitly declared in `requirements.txt` or `pyproject.toml` at the time this file was created.

## Architecture Notes

### Backend flow

The backend is organized around these cross-file flows:

```text
CLI / scheduler / API
  -> StockCollector / analyzers / trading engines
  -> AkshareMarketData and external market data sources
  -> MySQLRepository
  -> MySQL tables
```

Key backend files:

- `src/astocks_collector/cli.py`: argparse CLI and command wiring.
- `src/astocks_collector/config.py`: environment-based application configuration.
- `src/astocks_collector/db.py`: MySQL connection handling, main schema creation, and repository methods.
- `src/astocks_collector/market_data.py`: AKShare / Eastmoney / Sina data fetching and field normalization.
- `src/astocks_collector/collector.py`: basic stock sync, historical backfill, incremental collection.
- `src/astocks_collector/scheduler.py`: APScheduler daily incremental collection.
- `src/astocks_collector/analysis.py`: T+1 stock analysis.
- `src/astocks_collector/three_day_analysis.py`: future 3-trading-day trend analysis.
- `src/astocks_collector/realtime.py`: realtime quote analysis and regular simulation trading.
- `src/astocks_collector/llm_client.py`: OpenAI-compatible LLM client used by analysis and decision flows.

### API and T+1 routing

`src/astocks_collector/web_api.py` creates the FastAPI app and defines most `/api/...` routes.

T+1 routes are special: `src/astocks_collector/t1_api_routes.py` installs T+1 API routes via import side effects / monkey patching of `FastAPI.__init__`. When changing API behavior, do not inspect only `web_api.py`; also check `t1_api_routes.py`.

Important T+1 files:

- `src/astocks_collector/t1_trading.py`: T+1 simulation trading engine and T+1-specific account / position / order / trade tables.
- `src/astocks_collector/t1_task_manager.py`: long-running in-process T+1 realtime task manager and task status table.
- `src/astocks_collector/t1_api_routes.py`: T+1 dashboard, run, reset, task start, task stop, and 14:05 quality selection endpoints.
- `src/astocks_collector/t1_quality.py`: 14:05 intraday T+1 quality selection; uses realtime quotes, strict LLM review, and writes `stock_t1_intraday_*` tables.
- `src/astocks_collector/t1_quality_scheduler.py`: shared T+1 quality-selection job for both `serve-api` and `scheduler` entrypoints.

Regular realtime simulation uses `simulation_*` tables. T+1 simulation uses separate `t1_simulation_*` tables. Do not mix these paths.

### Database responsibilities

- Main schema and most repository operations: `src/astocks_collector/db.py`.
- T+1 simulation tables: `src/astocks_collector/t1_trading.py`.
- T+1 long-task status table: `src/astocks_collector/t1_task_manager.py`.
- T+1 14:05 quality-selection audit tables: `stock_t1_intraday_run` and `stock_t1_intraday_pick` in `db.py`.

When changing schema or table usage, check initialization paths, API readers, CLI commands, and README table notes together.

### Frontend structure

- `web/src/App.tsx`: main dashboard and route-level UI state.
- `web/src/api.ts`: shared frontend API helpers for most backend endpoints.
- `web/src/T1TradingSection.tsx`: T+1 page; it defines its own request helper and calls T+1 endpoints directly.
- `web/src/components/AntvChart.tsx`: chart rendering.
- `web/vite.config.ts`: Vite dev proxy for `/api`.

When changing API response shapes, update both backend route code and the relevant frontend call sites. For T+1 endpoints, check `T1TradingSection.tsx` even if `api.ts` looks unchanged.

## Maintenance Notes

- Do not read, copy, or commit real `.env` contents. Use `.env.example` and README for configuration shape.
- Avoid treating local or generated directories as source: `.claude`, `.omc`, `.venv`, `node_modules`, `web/dist`, `logs`, `reports`, `__pycache__`, and `*.egg-info`.
- If modifying CLI commands, update `src/astocks_collector/cli.py` and cross-check README command examples.
- If modifying API behavior, check `web_api.py`, `t1_api_routes.py`, `web/src/api.ts`, and the relevant React component.
- If modifying T+1 long-running behavior, consider all of `t1_task_manager.py`, `t1_trading.py`, `t1_api_routes.py`, and `web/src/T1TradingSection.tsx`.
- If modifying 14:05 T+1 quality selection, keep `t1_quality.py`, `t1_quality_scheduler.py`, `db.py`, `t1_trading.py`, `t1_api_routes.py`, and `web/src/T1TradingSection.tsx` in sync; do not store intraday quality picks in `stock_analysis_pick`.
- If modifying market data fields or upstream fallbacks, start with `market_data.py`; downstream modules expect normalized field names.
- Financial and LLM analysis outputs are for strategy research and simulation only; do not present them as investment advice.
