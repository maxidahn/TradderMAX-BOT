"""
Celerity Trader Bot - Persistence Layer
========================================
Saves and restores trade history, open positions, and performance data
across bot restarts. Uses JSON for human-readable storage.

Files saved to data/ directory:
  - trade_history.json   : all completed trades + P&L
  - open_positions.json  : any positions open at time of shutdown
  - ml_feedback.json     : labeled trade outcomes for ML retraining
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List

logger = logging.getLogger("celerity.persistence")

# Railway mounts a persistent volume at /data and sets DATA_DIR=/data.
# Locally falls back to the "data/" subfolder.
DATA_DIR = os.getenv("DATA_DIR", "data")
TRADES_FILE    = os.path.join(DATA_DIR, "trade_history.json")
POSITIONS_FILE = os.path.join(DATA_DIR, "open_positions.json")
FEEDBACK_FILE  = os.path.join(DATA_DIR, "ml_feedback.json")
RISK_FILE      = os.path.join(DATA_DIR, "risk_level.json")
PAIRS_FILE     = os.path.join(DATA_DIR, "pair_states.json")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ─── Trade History ───

def save_trade_history(trade_history: list):
    """Persist all completed trades to disk."""
    _ensure_dir()
    data = [
        {
            "symbol":        t.symbol,
            "side":          t.side,
            "price":         t.price,
            "quantity":      t.quantity,
            "usdt_amount":   t.usdt_amount,
            "pnl":           t.pnl,
            "pnl_pct":       t.pnl_pct,
            "pnl_gross":     t.pnl_gross,
            "total_fees":    t.total_fees,
            "slippage_cost": t.slippage_cost,
            "reason":        t.reason,
            "timestamp":     t.timestamp,
            "order_id":      t.order_id,
        }
        for t in trade_history
    ]
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Persistence: saved {len(data)} trades → {TRADES_FILE}")


def load_trade_history() -> List[dict]:
    """Load trade history from disk. Returns list of dicts."""
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        logger.info(f"Persistence: loaded {len(data)} trades from {TRADES_FILE}")
        return data
    except Exception as e:
        logger.error(f"Persistence: failed to load trade history: {e}")
        return []


# ─── Open Positions ───

def save_open_positions(positions: dict):
    """Persist any open positions so they survive restarts."""
    _ensure_dir()
    data = {
        sym: {
            "symbol":      pos.symbol,
            "side":        pos.side,
            "entry_price": pos.entry_price,
            "quantity":    pos.quantity,
            "usdt_amount": pos.usdt_amount,
            "entry_time":  pos.entry_time,
            "order_id":    pos.order_id,
            "entry_fee":   pos.entry_fee,
            "peak_price":  pos.peak_price,  # Trailing stop anchor — survives restarts
        }
        for sym, pos in positions.items()
    }
    with open(POSITIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    if data:
        logger.info(f"Persistence: saved {len(data)} open positions → {POSITIONS_FILE}")


def load_open_positions() -> Dict[str, dict]:
    """Load open positions from disk. Returns dict of symbol → position data."""
    if not os.path.exists(POSITIONS_FILE):
        return {}
    try:
        with open(POSITIONS_FILE) as f:
            data = json.load(f)
        if data:
            logger.info(f"Persistence: restored {len(data)} open positions from {POSITIONS_FILE}")
        return data
    except Exception as e:
        logger.error(f"Persistence: failed to load positions: {e}")
        return {}


def clear_open_positions():
    """Called after a position is closed — remove from disk."""
    save_open_positions({})


# ─── ML Feedback (trade outcomes for retraining) ───

def save_ml_feedback(entry: dict):
    """
    Save a labeled trade outcome for ML model improvement.
    entry = {
        "symbol": str,
        "entry_time": str,
        "exit_time": str,
        "ai_score_at_entry": float,
        "pnl_pct": float,
        "profitable": bool,
        "regime": str,
        "features": dict  (optional: RSI, volume_ratio, etc.)
    }
    """
    _ensure_dir()
    feedback = []
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE) as f:
                feedback = json.load(f)
        except Exception:
            feedback = []

    entry["saved_at"] = datetime.now(timezone.utc).isoformat()
    feedback.append(entry)

    with open(FEEDBACK_FILE, "w") as f:
        json.dump(feedback, f, indent=2)
    logger.info(f"Persistence: saved ML feedback entry ({len(feedback)} total)")


def load_ml_feedback() -> List[dict]:
    """Load all ML feedback entries."""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    try:
        with open(FEEDBACK_FILE) as f:
            return json.load(f)
    except Exception:
        return []


# ─── Risk Level ───

def save_risk_level(level: int):
    """Persist the selected risk level so it survives restarts."""
    _ensure_dir()
    with open(RISK_FILE, "w") as f:
        json.dump({"risk_level": int(level)}, f)
    logger.info(f"Persistence: saved risk_level={level}")


def load_risk_level(default: int = 5) -> int:
    """Load persisted risk level, or return default if not found."""
    if not os.path.exists(RISK_FILE):
        return default
    try:
        with open(RISK_FILE) as f:
            return int(json.load(f).get("risk_level", default))
    except Exception as e:
        logger.warning(f"Persistence: could not load risk_level ({e}), using default {default}")
        return default


# ─── Pair States ───

def save_pair_states(trading_pairs: list):
    """Persist enabled/disabled state of each trading pair."""
    _ensure_dir()
    data = {p.symbol: p.enabled for p in trading_pairs}
    with open(PAIRS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Persistence: saved pair states → {data}")


def load_pair_states() -> dict:
    """Load persisted pair states. Returns {symbol: enabled} dict."""
    if not os.path.exists(PAIRS_FILE):
        return {}
    try:
        with open(PAIRS_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Persistence: could not load pair states ({e})")
        return {}


def get_performance_summary() -> dict:
    """
    Compute performance stats from saved trade history.
    Useful for reporting on restart.
    """
    trades = load_trade_history()
    if not trades:
        return {"total_trades": 0, "message": "No trades recorded yet"}

    total_pnl    = sum(t["pnl"] for t in trades)
    win_trades   = [t for t in trades if t["pnl"] > 0]
    total_fees   = sum(t.get("total_fees", 0) for t in trades)
    best_trade   = max(trades, key=lambda t: t["pnl"])
    worst_trade  = min(trades, key=lambda t: t["pnl"])

    return {
        "total_trades":  len(trades),
        "winning_trades": len(win_trades),
        "win_rate":      round(len(win_trades) / len(trades) * 100, 1),
        "total_pnl":     round(total_pnl, 4),
        "total_fees":    round(total_fees, 4),
        "best_trade":    {"symbol": best_trade["symbol"], "pnl": round(best_trade["pnl"], 4)},
        "worst_trade":   {"symbol": worst_trade["symbol"], "pnl": round(worst_trade["pnl"], 4)},
        "first_trade":   trades[0]["timestamp"],
        "last_trade":    trades[-1]["timestamp"],
    }
