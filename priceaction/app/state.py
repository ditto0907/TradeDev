from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, List, Optional

from fastapi import WebSocket

from analysis.price_action_analyzer import PriceActionAnalyzer
from integrations.google_sheets_sync import GoogleSheetsSync
from marketdata.ib_fetcher import IBDataFetcher
from trading.order_manager import IBOrderManager

IB_COOLDOWN_NO_DATA = 300
IB_COOLDOWN_ERROR = 60
MES_SYM = "MES"


@dataclass
class AppState:
    fetcher: IBDataFetcher = field(default_factory=IBDataFetcher)
    sheets: GoogleSheetsSync = field(default_factory=GoogleSheetsSync)
    analyzer: PriceActionAnalyzer = field(default_factory=PriceActionAnalyzer)
    order_mgr: Optional[IBOrderManager] = None
    ws_clients: List[WebSocket] = field(default_factory=list)
    latest_analysis: dict = field(default_factory=dict)
    last_analysis_bar_ts: int = 0
    prev_completed_bar: dict = field(default_factory=dict)
    ib_fetch_cooldown: dict = field(default_factory=dict)

    IB_COOLDOWN_NO_DATA: ClassVar[int] = IB_COOLDOWN_NO_DATA
    IB_COOLDOWN_ERROR: ClassVar[int] = IB_COOLDOWN_ERROR
    MES_SYM: ClassVar[str] = MES_SYM
