# PriceAction 目录重构方案

> **状态**：Current — Phase 1 已实施（`app/`、`core/`、`marketdata/`、`storage/`、`trading/` 等目录已落地），Phase 2/3 待推进
> **日期**：2026-08-31
> **上游**：[v1_system_plan.md](v1_system_plan.md)

## 背景

`priceaction/` 目录当前约有 20 个 Python 模块平铺在根目录，职责边界不清晰，后续继续演进会让 import、启动入口、测试维护成本持续上升。该方案用于约束后续目录整理，并作为 Phase 2 / Phase 3 的实施依据。

## 现状问题

- 根目录平铺 ~20 个 Python 模块，无法一眼区分数据层、交易层、分析层、接入层。
- `server.py` 是上帝文件（约 115KB）：路由、中间件、生命周期、WebSocket、统计计算、直接 SQL 都混在一个文件里。
- 命名不一致：已经存在 `strategy/` 目录，但 `strategy_backtest.py` 仍放在根目录。
- 前端 `app.js` 是 136KB 单文件，静态资源职责也未分组。
- 运行产物（`data/`、`log/`、`credentials/`）与源码同级，需要明确区分并通过 `.gitignore` 做保护。

## 目标结构

```text
priceaction/
├── main.py                      # 入口：uvicorn app 装配
├── app/                         # Web 接入层（FastAPI）
│   ├── __init__.py              # create_app() 工厂 + 中间件注册
│   ├── lifespan.py              # 生命周期（IB init、后台任务、重连）
│   ├── middleware.py            # token auth + datafeed debug
│   ├── websocket.py             # /ws/realtime + broadcast()
│   └── routers/                 # udf.py orders.py analysis.py skill.py charts.py strategy.py datavalid.py trades.py
├── core/                        # config.py, logging_setup.py
├── marketdata/                  # ib_fetcher.py, realtime_builder.py, data_manager.py, continuous_view.py, data_validator.py, trading_calendar.py, contract_calendar.py, market_holidays.py
├── storage/                     # db.py
├── trading/                     # order_manager.py, trade_log_parser.py, trade_stats.py
├── analysis/                    # price_action_analyzer.py
├── strategy/                    # backtest.py（原 strategy_backtest.py）+ 现有 strategy/ 内容
├── integrations/                # google_sheets_sync.py, mcp_market_cycle.py, mcp_strategy_research.py, ib_log_translator.py
├── static/                      # html 保持；js 移入 static/js/
├── scripts/ tests/ doc/         # 保持
└── data/ log/ credentials/      # 运行产物，保持并确保 .gitignore 保护
```

## 三阶段实施计划

### Phase 1（本 PR）

目标：只做目录落位，不改业务逻辑。

- 新建 `app/`、`core/`、`marketdata/`、`storage/`、`trading/`、`analysis/`、`integrations/` 等包目录。
- 按目标结构移动现有 Python 模块，并修正所有 import（含 `tests/` 与 `scripts/` 内路径）。
- 将 `server.py` 主体移动到 `app/server.py`，但保留根目录 `server.py` shim，继续兼容 `uvicorn server:app` 与 `scripts/start_server.sh`。
- 将 `static/app.js`、`static/datafeed.js`、`static/timezone.js` 移到 `static/js/`，同步更新 HTML 引用。
- 更新 `README.md` 中的模块路径、Mermaid 图和快速启动说明。
- 验证 `priceaction/tests` 下 unittest 可通过，并确认 Python 模块能被正常 import。

**约束**：Phase 1 不拆 `server.py`、不重命名函数/类、不调整业务流程。

### Phase 2 ✅（已完成）

目标：拆分接入层职责，收敛全局状态。

- ✅ 将 `app/server.py` 中的 REST API 按业务域拆到 `app/routers/`：`udf.py`、`orders.py`、`skill.py`、`charts.py`、`strategy.py`、`datavalid.py`、`trades.py`。
- ✅ 抽离生命周期、WebSocket、中间件到 `app/lifespan.py`、`app/websocket.py`、`app/middleware.py`。
- ✅ 将模块级全局变量（`fetcher`、`_ws_clients`、`_order_mgr`、cooldown map 等）收敛到 `app/state.py` 的 `AppState` 对象，通过 `request.app.state.app_state` 访问。
- ✅ 从 `server.py` 中抽出 `trading/trade_stats.py` 纯领域逻辑。
- ✅ `app/server.py` 从 2853 行精简至 64 行（仅剩薄编排层）。
- ✅ 验证：`python -m unittest discover` 通过（94 tests OK），`from app.server import app` 可正常 import。

### Phase 3

目标：整理前端与遗留文档。

- 将 `static/js/app.js` 继续拆为 ES modules（如 chart / orders / watchlist / panels / annotations 等）。
- 处理 README 中提到但仓库里已不存在的 `test_data.py` 说明，同步修正文档与图示。
- 清理文档中的旧路径、过时命令和已失效的架构描述，确保新结构与说明一致。

## Phase 1 执行范围

1. 按目标结构移动文件到新目录（Python 包需补 `__init__.py`）。
2. 修正所有 import，包括 `tests/` 与 `scripts/`。
3. 保留根目录 `server.py` shim 兼容旧启动命令。
4. 将前端 JS 文件移入 `static/js/` 并更新各 HTML 引用。
5. 更新 `README.md` 中的模块路径、Mermaid 图、快速启动说明。
6. 将 `strategy_backtest.py` 改名为 `strategy/backtest.py`，与现有 `strategy/` 目录并存。
7. 不改任何业务逻辑，不重命名函数/类。
8. 验证：`python -m unittest discover` 可通过，且 Python 文件可正常 import。

## 验收标准

- `priceaction/doc/architecture/v1_directory_refactor_2026-08.md` 存在并包含完整三阶段方案。
- 目录结构符合 Phase 1 的目标布局。
- 所有 import 已修正，根目录 `server.py` shim 可继续工作。
- `README.md` 中路径、图示、启动说明已同步更新。
- `priceaction/tests` 下 unittest 通过。
