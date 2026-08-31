# 文档库总索引

本目录按主题分类，文件名使用 `v{N}_主题.md` 版本前缀，按名称排序即按迭代顺序阅读。
每篇文档头部有统一元数据块：**状态**（Current / Implemented / Superseded / Historical / Snapshot）、**日期**、**上下游链接**。

## 目录结构

### architecture — 系统级架构与重构
| 文档 | 状态 | 说明 |
|---|---|---|
| [v1_system_plan.md](architecture/v1_system_plan.md) | Superseded | 初版系统实施计划（2026-04-09） |
| [v1_directory_refactor_2026-08.md](architecture/v1_directory_refactor_2026-08.md) | Current | `priceaction/` 目录分层方案，Phase 1 已实施 |

### data — 数据层设计迭代（主线最完整的模块）
| 文档 | 状态 | 说明 |
|---|---|---|
| [v1_dataflow.md](data/v1_dataflow.md) | Living | 端到端数据流全景（Mermaid） |
| [v2_kline_arch_refactor.md](data/v2_kline_arch_refactor.md) | Implemented | K 线架构服务化重构设计 |
| [v2_migration.md](data/v2_migration.md) | Historical | v1→v2 迁移说明 |
| [v3_data_redesign.md](data/v3_data_redesign.md) | Current | per-contract fact + derived view 重设计，已落地 |

### modules — 代码走读（快照类）
| 文档 | 状态 | 说明 |
|---|---|---|
| [server.md](modules/server.md) | Snapshot 04-17 | FastAPI 编排层（现已拆分为 `app/`） |
| [ib_data_fetcher.md](modules/ib_data_fetcher.md) | Snapshot 04-17 | IB 接入层（现 `marketdata/ib_fetcher.py`） |
| [data_validator.md](modules/data_validator.md) | Snapshot 04-17 | 数据对账层（现 `marketdata/data_validator.py`） |

### strategy — 策略研究迭代
| 文档 | 状态 | 说明 |
|---|---|---|
| [v0_requirement.md](strategy/v0_requirement.md) | Historical | 原始需求记录 |
| [v1_ibs_backtest.md](strategy/v1_ibs_backtest.md) | Implemented | IBS 2-bar 回测引擎 + Strategy Tab |
| [v2_strategy_research.md](strategy/v2_strategy_research.md) | Current | 信号扫描 + MCP/LLM 分工 + Google Sheet |

### skills — Skill & MCP 设计
| 文档 | 状态 | 说明 |
|---|---|---|
| [v1_gap_analysis_skill_design.md](skills/v1_gap_analysis_skill_design.md) | Current | Gap Analysis Skill 设计定稿 |
| [v1_gap_analysis_implementation_design.md](skills/v1_gap_analysis_implementation_design.md) | 编码进行中 | 落地实现方案（Skill / MCP / 测试） |

> 已上线 skill 的操作手册在 `.github/skills/`（market-cycle-analysis、bar-by-bar-analysis、strategy-research）；本目录存设计稿与迭代记录。

### reference — 外部技术参考（纯查阅，不迭代）
- tradingview/：[v1_readme.md](reference/tradingview/v1_readme.md)（入口索引）、[v1_official_docs_index.md](reference/tradingview/v1_official_docs_index.md)、[v1_project_usage.md](reference/tradingview/v1_project_usage.md)、[v1_charting_library_components.md](reference/tradingview/v1_charting_library_components.md)
- ib/：[v1_tick_data_guide.md](reference/ib/v1_tick_data_guide.md)

## 迭代时间线

### 数据层
```
04-09  v1_system_plan          系统初版：IB → SQLite → TradingView 管道成型
04-14  v1_dataflow             数据流全景文档化
04-16  v2_kline_arch_refactor  根因重构：trading calendar / 统一 tick 路径 / 入库校验
04-21  v2_migration            v1→v2 迁移完成（user_version=2）
05-02  v3_data_redesign        per-contract bars 唯一事实 + 连续合约读时合成（已落地）
```

### 策略层
```
04-12  v0_requirement          2-bar IBS 信号 + 背景过滤需求
04-13  v1_ibs_backtest         回测引擎 + Strategy Tab（已实施）
05-07  v2_strategy_research    MCP 量化 + LLM 背景分析 + 人工评价流水线（已实施）
```

### Skill 层
```
（前置） market-cycle-analysis / bar-by-bar-analysis / strategy-research 已上线（.github/skills/）
08-31  v1_gap_analysis_skill_design           G3/G4/G5 结构性缺口记分板设计（定稿）
08-31  v1_gap_analysis_implementation_design  P1/P2/P3 分期落地方案（编码进行中）
```

### 架构层
```
04-09  v1_system_plan                   单目录平铺起步
08-31  v1_directory_refactor_2026-08    app/ core/ marketdata/ storage/ 分层（Phase 1 已实施）
```

## 维护约定

- 新文档：放入对应主题目录，版本号递增（`v{N}_主题.md`），并在本索引登记。
- 方案被替代时：不删除，在头部元数据标 `Superseded by ...` 并链接后继文档。
- 走读类文档：标注快照日期，不追着代码持续改；大版本重构后新写一篇。
