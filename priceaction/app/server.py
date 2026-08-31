"""
FastAPI server — TradingView UDF REST API, WebSocket real-time feed,
SQLite persistence, and IB order submission.

Start with:
    uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
import logging.handlers
from pathlib import Path

from fastapi import FastAPI

from app.lifespan import lifespan
from app.middleware import add_middlewares
from app.routers import charts, datavalid, orders, skill, strategy, trades, udf
from app.state import AppState
from app.websocket import router as websocket_router
from core import config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = PROJECT_ROOT / "log"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=str(_LOG_DIR / "server.log"),
    when="H",
    interval=1,
    backupCount=168,
    encoding="utf-8",
    utc=True,
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_file_handler.suffix = "%Y%m%d_%H"
logging.getLogger().addHandler(_file_handler)

app_state = AppState()
app = FastAPI(title="MES Price Action Server", lifespan=lifespan)
app.state.app_state = app_state

add_middlewares(app)
udf.mount_static_files(app)

app.include_router(udf.router)
app.include_router(skill.router)
app.include_router(datavalid.router)
app.include_router(trades.router)
app.include_router(orders.router)
app.include_router(charts.router)
app.include_router(strategy.router)
app.include_router(websocket_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                reload=False, loop="asyncio")
