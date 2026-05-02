"""
Continuous-contract view — derived series assembled at read time from
per-contract ``bars`` rows.  Per design ``doc/data_redesign_v3.md`` §2,
**we never persist a continuous series as a fact** — IB ContFuture data
is back-adjusted on every rollover, so a stored continuous bar would have
to be rewritten constantly.  Instead we keep monthly bars as immutable
facts and stitch them on the fly.

Three series methods are supported:

* ``front``           — switch to the next contract on each rollover_date.
                        Prices are kept as-is — there will be gaps at
                        rollover.  This is what most market-cycle / TR
                        analysis wants because real prices are preserved.
* ``cont_ratio``      — apply a multiplicative back-adjustment so the
                        rollover-day close of the *new* contract equals
                        the rollover-day close of the *old* contract.
                        All earlier bars are scaled by ``new_close /
                        old_close``.
* ``cont_difference`` — apply an additive back-adjustment so the
                        rollover-day close of the *new* contract equals
                        the rollover-day close of the *old* contract.
                        All earlier bars are shifted by
                        ``new_close - old_close``.

Public API:

    assemble_continuous(symbol, timeframe, from_ts, to_ts, method='front')
        Returns a list of bar dicts (time, open, high, low, close,
        volume, contract_month) — sorted ascending by time.  Volume from
        each contract is preserved unchanged.
"""
from __future__ import annotations

from typing import Dict, List, Literal

import db
from contract_calendar import active_contract

Method = Literal["front", "cont_ratio", "cont_difference"]


def _select_front_bars(
    bars_by_cm: Dict[str, List[dict]],
    symbol: str,
) -> List[dict]:
    """For each timestamp present across contracts, keep only the bar from
    the contract that is the front month at that timestamp.

    *bars_by_cm* maps contract_month → bars (time-sorted, no overlap inside
    a single contract).  Output is sorted ascending by time.
    """
    selected: List[dict] = []
    for cm, bars in bars_by_cm.items():
        for b in bars:
            front = active_contract(int(b["time"]), symbol)
            if front == cm:
                selected.append(b)
    selected.sort(key=lambda b: b["time"])
    return selected


def _adjustment_factors(
    bars_by_cm: Dict[str, List[dict]],
    contract_order: List[str],
    method: Method,
) -> Dict[str, float]:
    """Compute the cumulative back-adjustment factor for each contract.

    The latest contract has factor 0 (additive) or 1.0 (ratio) — its
    bars are emitted unchanged.  Each earlier contract's factor is
    derived from the rollover-day boundary closes.

    For ``cont_ratio``:   factor[old]  = factor[next] * (new_close /
                                                          old_close)
    For ``cont_difference``: factor[old] = factor[next] + (new_close -
                                                            old_close)

    "old_close" = the last close in *bars_by_cm[old]* whose timestamp is
    < the first timestamp of *bars_by_cm[next]*.
    "new_close" = the first close in *bars_by_cm[next]* at or after the
    rollover boundary.

    If a boundary cannot be determined (one of the contracts is empty or
    they don't overlap in time), the factor is propagated unchanged
    (i.e. that contract's prices will appear without adjustment).  This
    is intentional — we don't fabricate data.
    """
    if method == "front":
        return {cm: (0.0 if False else 0.0) for cm in contract_order}

    if method == "cont_ratio":
        identity = 1.0
    else:  # cont_difference
        identity = 0.0

    factors: Dict[str, float] = {cm: identity for cm in contract_order}
    if len(contract_order) < 2:
        return factors

    # Walk back from latest → earliest.
    for i in range(len(contract_order) - 1, 0, -1):
        new_cm = contract_order[i]
        old_cm = contract_order[i - 1]
        new_bars = bars_by_cm.get(new_cm, [])
        old_bars = bars_by_cm.get(old_cm, [])
        if not new_bars or not old_bars:
            factors[old_cm] = factors[new_cm]
            continue
        new_first_ts = new_bars[0]["time"]
        # Find the most recent old bar at or before new_first_ts
        old_close = None
        for b in reversed(old_bars):
            if b["time"] <= new_first_ts:
                old_close = float(b["close"])
                break
        new_close = float(new_bars[0]["close"])
        if old_close is None or old_close == 0:
            factors[old_cm] = factors[new_cm]
            continue
        if method == "cont_ratio":
            factors[old_cm] = factors[new_cm] * (new_close / old_close)
        else:
            factors[old_cm] = factors[new_cm] + (new_close - old_close)
    return factors


def _apply_adjustment(bar: dict, factor: float, method: Method) -> dict:
    """Return a new bar dict with prices adjusted by *factor*."""
    out = dict(bar)
    if method == "cont_ratio":
        for k in ("open", "high", "low", "close"):
            out[k] = float(bar[k]) * factor
    elif method == "cont_difference":
        for k in ("open", "high", "low", "close"):
            out[k] = float(bar[k]) + factor
    return out


def assemble_continuous(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    method: Method = "front",
) -> List[dict]:
    """Assemble a continuous bar series from per-contract bars.

    Reads ``bars`` rows for every contract that overlaps [from_ts, to_ts]
    and stitches them according to *method*.  The result is **never
    persisted** — call this function on every request.
    """
    if method not in ("front", "cont_ratio", "cont_difference"):
        raise ValueError(f"unsupported method: {method!r}")

    # Pull all contracts present in the window.  We rely on the front-month
    # selection to discard rollover overlap; fetching every contract that
    # has any data in the window keeps the call simple.
    all_bars = db.get_bars(symbol, timeframe, from_ts=from_ts, to_ts=to_ts)
    if not all_bars:
        return []

    bars_by_cm: Dict[str, List[dict]] = {}
    for b in all_bars:
        cm = b.get("contract_month") or ""
        if not cm:
            # v3 should never store rows without contract_month.  Skip
            # defensively in case some legacy row sneaks through.
            continue
        bars_by_cm.setdefault(cm, []).append(b)
    for cm in bars_by_cm:
        bars_by_cm[cm].sort(key=lambda b: b["time"])

    # Step 1: front-month selection — discards rollover overlap.
    front_bars = _select_front_bars(bars_by_cm, symbol)
    if not front_bars or method == "front":
        return front_bars

    # Step 2: regroup the front-only bars by contract for adjustment-factor
    # computation (we need the per-contract first/last bar to find rollover
    # boundary closes).
    front_by_cm: Dict[str, List[dict]] = {}
    for b in front_bars:
        front_by_cm.setdefault(b["contract_month"], []).append(b)
    for cm in front_by_cm:
        front_by_cm[cm].sort(key=lambda b: b["time"])

    contract_order = sorted(front_by_cm.keys())
    factors = _adjustment_factors(front_by_cm, contract_order, method)

    out: List[dict] = []
    for cm in contract_order:
        f = factors[cm]
        for b in front_by_cm[cm]:
            out.append(_apply_adjustment(b, f, method))
    out.sort(key=lambda b: b["time"])
    return out


# ── Token routing helper ──────────────────────────────────────────────────────


def parse_token(token: str) -> dict:
    """Parse a chart symbol token into its routing components.

    Token grammar (per design §5):
      * ``SYMBOL@CONT_FRONT``   — continuous, no adjustment
      * ``SYMBOL@CONT_RATIO``   — continuous, ratio-adjusted
      * ``SYMBOL@CONT_DIFF``    — continuous, difference-adjusted
      * ``SYMBOL@YYYYMM``       — single-contract
      * ``SYMBOL``              — bare symbol → defaults to CONT_FRONT

    Returns ``{"symbol": str, "kind": "month"|"continuous",
              "contract_month": str|None, "method": Method|None}``
    """
    if not token:
        raise ValueError("empty token")
    if "@" not in token:
        return {"symbol": token, "kind": "continuous",
                "contract_month": None, "method": "front"}
    sym, suffix = token.split("@", 1)
    suffix = suffix.upper()
    if suffix == "CONT_FRONT":
        return {"symbol": sym, "kind": "continuous",
                "contract_month": None, "method": "front"}
    if suffix == "CONT_RATIO":
        return {"symbol": sym, "kind": "continuous",
                "contract_month": None, "method": "cont_ratio"}
    if suffix in ("CONT_DIFF", "CONT_DIFFERENCE"):
        return {"symbol": sym, "kind": "continuous",
                "contract_month": None, "method": "cont_difference"}
    # YYYYMM
    if len(suffix) == 6 and suffix.isdigit():
        return {"symbol": sym, "kind": "month",
                "contract_month": suffix, "method": None}
    raise ValueError(f"unrecognized token suffix: {suffix!r}")
