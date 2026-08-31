import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.state import AppState

logger = logging.getLogger(__name__)

router = APIRouter()


async def broadcast(state: AppState, message: dict):
    payload = json.dumps(message)
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.remove(ws)


@router.websocket("/ws/realtime")
async def websocket_endpoint(websocket: WebSocket):
    state = websocket.app.state.app_state

    await websocket.accept()
    state.ws_clients.append(websocket)
    logger.info("WebSocket client connected (total: %d)", len(state.ws_clients))

    try:
        snapshot_bars = state.fetcher.get_bars("5min")[-200:]
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "bars_5min": snapshot_bars,
            "analysis": state.latest_analysis,
        }))
    except Exception as e:
        logger.warning("Snapshot send failed: %s", e)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.ws_clients:
            state.ws_clients.remove(websocket)
        logger.info("WebSocket client disconnected (total: %d)", len(state.ws_clients))
