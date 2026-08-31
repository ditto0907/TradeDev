from typing import List


def compute_tradelog_stats(rows: List[dict]) -> dict:
    """Compute analytics dashboard metrics from a list of trade_logs rows."""
    closed = [r for r in rows if r.get("pnl") is not None and r.get("exit_time")]
    total = len(closed)
    if total == 0:
        return {
            "total": 0, "total_pnl": 0,
            "wins": 0, "losses": 0, "win_rate": 0,
            "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
            "rr": 0, "best_trade": None, "worst_trade": None,
            "long_count": 0, "short_count": 0,
            "long_pct": 0, "short_pct": 0,
            "winning_days": 0, "losing_days": 0, "breakeven_days": 0,
            "total_days": 0, "day_win_pct": 0,
            "daily_pnl": [], "cumulative_pnl": [],
            "duration_buckets": [], "winrate_buckets": [],
            "best_day_pct": 0,
        }
    wins = [r for r in closed if r["pnl"] >= 0]
    losses = [r for r in closed if r["pnl"] < 0]
    total_win = sum(r["pnl"] for r in wins)
    total_loss = -sum(r["pnl"] for r in losses)
    total_pnl = sum(r["pnl"] for r in closed)
    avg_win = total_win / len(wins) if wins else 0
    avg_loss = total_loss / len(losses) if losses else 0
    rr = (avg_win / avg_loss) if avg_loss > 0 else 0
    pf = (total_win / total_loss) if total_loss > 0 else 0
    win_rate = len(wins) / total * 100

    best = max(closed, key=lambda r: r["pnl"])
    worst = min(closed, key=lambda r: r["pnl"])

    daily: dict = {}
    for r in closed:
        d = r.get("date") or ""
        daily[d] = daily.get(d, 0) + r["pnl"]
    daily_sorted = sorted(daily.items())
    daily_pnl = [{"date": d, "pnl": round(v, 2)} for d, v in daily_sorted]
    cum = 0
    cumulative_pnl = []
    for d, v in daily_sorted:
        cum += v
        cumulative_pnl.append({"date": d, "pnl": round(cum, 2)})

    best_day_pct = 0
    if total_pnl > 0:
        best_day = max((v for _, v in daily_sorted if v > 0), default=0)
        best_day_pct = round((best_day / total_pnl) * 100, 2) if best_day else 0

    winning_days = sum(1 for _, v in daily_sorted if v > 0)
    losing_days = sum(1 for _, v in daily_sorted if v < 0)
    breakeven_days = sum(1 for _, v in daily_sorted if v == 0)
    total_days = len(daily_sorted)
    day_win_pct = round(winning_days / total_days * 100, 2) if total_days else 0

    bucket_defs = [
        ("Under 15 sec", 0, 15),
        ("15-45 sec", 15, 45),
        ("45 sec - 1 min", 45, 60),
        ("1 min - 2 min", 60, 120),
        ("2 min - 5 min", 120, 300),
        ("5 min - 10 min", 300, 600),
        ("10 min - 30 min", 600, 1800),
        ("30 min - 1 hour", 1800, 3600),
        ("1 hour - 2 hours", 3600, 7200),
        ("2 hours - 4 hours", 7200, 14400),
        ("4 hours and up", 14400, 10**9),
    ]
    durations = []
    for r in closed:
        if r.get("entry_time") and r.get("exit_time"):
            durations.append((r["exit_time"] - r["entry_time"], r["pnl"]))
    duration_buckets = []
    winrate_buckets = []
    for label, lo, hi in bucket_defs:
        in_b = [d for d in durations if lo <= d[0] < hi]
        cnt = len(in_b)
        wins_b = sum(1 for d in in_b if d[1] >= 0)
        wr = round((wins_b / cnt) * 100, 2) if cnt else 0
        duration_buckets.append({"label": label, "count": cnt})
        winrate_buckets.append({"label": label, "win_rate": wr})

    long_count = sum(1 for r in closed if r["direction"] == "long")
    short_count = sum(1 for r in closed if r["direction"] == "short")

    return {
        "total": total,
        "total_pnl": round(total_pnl, 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(pf, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr": round(rr, 2),
        "best_trade": {"pnl": round(best["pnl"], 2), "symbol": best.get("symbol"),
                       "direction": best.get("direction"),
                       "entry_time": best.get("entry_time"),
                       "exit_time": best.get("exit_time"),
                       "entry_price": best.get("entry_price"),
                       "exit_price": best.get("exit_price")},
        "worst_trade": {"pnl": round(worst["pnl"], 2), "symbol": worst.get("symbol"),
                        "direction": worst.get("direction"),
                        "entry_time": worst.get("entry_time"),
                        "exit_time": worst.get("exit_time"),
                        "entry_price": worst.get("entry_price"),
                        "exit_price": worst.get("exit_price")},
        "long_count": long_count,
        "short_count": short_count,
        "long_pct": round(long_count / total * 100, 2),
        "short_pct": round(short_count / total * 100, 2),
        "best_day_pct": best_day_pct,
        "winning_days": winning_days,
        "losing_days": losing_days,
        "breakeven_days": breakeven_days,
        "total_days": total_days,
        "day_win_pct": day_win_pct,
        "daily_pnl": daily_pnl,
        "cumulative_pnl": cumulative_pnl,
        "duration_buckets": duration_buckets,
        "winrate_buckets": winrate_buckets,
    }
