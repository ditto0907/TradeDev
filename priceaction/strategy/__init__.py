"""Strategy research package."""

from .gap_detector import (
    Gap,
    GapEvent,
    G5Heartbeat,
    analyze_day,
    detect_g3_micro_gaps,
    detect_g4_breakout_gaps,
    detect_g5_ema_gap_bars,
    track_gap_lifecycle,
)
