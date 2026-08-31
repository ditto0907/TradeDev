import json
import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from core import config

DATAFEED_DEBUG = os.environ.get("DATAFEED_DEBUG", "0") == "1"
datafeed_logger = logging.getLogger("datafeed")
if DATAFEED_DEBUG:
    datafeed_logger.setLevel(logging.DEBUG)

_PROTECTED_PREFIXES = (
    "/api/order", "/api/flatten", "/api/data/delete", "/api/data/fix",
    "/api/data/validate_all", "/api/data/bg_validate", "/api/trades/upload",
    "/api/strategy/backtest",
)
_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def api_token_middleware(request: Request, call_next):
    token = config.API_TOKEN
    if token:
        path = request.url.path
        needs_auth = (
            request.method in _PROTECTED_METHODS
            or any(path.startswith(p) for p in _PROTECTED_PREFIXES)
        )
        if needs_auth and not (path.startswith("/static") or path.startswith("/charting_library")):
            provided = request.headers.get("X-API-Token") or request.query_params.get("token")
            if provided != token:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


async def datafeed_debug_middleware(request: Request, call_next):
    if DATAFEED_DEBUG and request.url.path.startswith("/api"):
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        _LAYOUT_PREFIXES = ("/api/charts", "/api/chart_templates",
                            "/api/study_templates", "/api/drawing_templates")
        if any(request.url.path.startswith(p) for p in _LAYOUT_PREFIXES):
            datafeed_logger.debug("%-6s %-40s → %d (%.1f ms) [body omitted]",
                                  request.method,
                                  str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
                                  response.status_code, elapsed_ms)
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            payload = json.loads(body)
            summary = {}
            for k, v in payload.items():
                if isinstance(v, list) and len(v) > 6:
                    summary[k] = f"[{v[0]!r}…{v[-1]!r}]({len(v)})"
                elif isinstance(v, str) and len(v) > 200:
                    summary[k] = f"({len(v)} chars)"
                else:
                    summary[k] = v
            body_log = json.dumps(summary)
        except Exception:
            body_log = body.decode(errors="replace")[:300]
        datafeed_logger.debug("%-6s %-40s → %d (%.1f ms) %s",
                              request.method,
                              str(request.url.path) + ("?" + str(request.url.query) if request.url.query else ""),
                              response.status_code, elapsed_ms, body_log)
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    return await call_next(request)


def add_middlewares(app: FastAPI):
    app.middleware("http")(api_token_middleware)
    app.middleware("http")(datafeed_debug_middleware)
