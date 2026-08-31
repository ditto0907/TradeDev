"""Shared FastAPI dependency helpers for all routers."""
from fastapi import Request

from app.state import AppState


def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state
