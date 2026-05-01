"""
Celerity Trader Bot - TradingView Webhook Integration
=======================================================
Receives alerts from TradingView via webhooks and converts them
into signals for the strategy engine (5th intelligence layer).

TradingView Alert Setup:
  1. Create an alert on your indicator/strategy in TradingView
  2. Set Webhook URL to: http://YOUR_SERVER:5000/api/tv/webhook
  3. Set the alert message body to JSON format (see EXPECTED_FORMATS below)

Supported alert message formats:

  FORMAT 1 — Simple (just action + symbol):
    {
      "secret": "celerity_tv_2024",
      "symbol": "BTCUSDT",
      "action": "BUY",
      "price": {{close}}
    }

  FORMAT 2 — With indicator data:
    {
      "secret": "celerity_tv_2024",
      "symbol": "BTCUSDT",
      "action": "BUY",
      "price": {{close}},
      "indicator": "EMA Cross",
      "timeframe": "5m",
      "confidence": 0.8,
      "message": "EMA 9 crossed above EMA 21"
    }

  FORMAT 3 — Full strategy report:
    {
      "secret": "celerity_tv_2024",
      "symbol": "BTCUSDT",
      "action": "STRONG_BUY",
      "price": {{close}},
      "indicator": "Multi-Indicator Strategy",
      "timeframe": "5m",
      "confidence": 0.9,
      "rsi": {{plot("RSI")}},
      "volume": {{volume}},
      "message": "All conditions aligned: RSI bounce + Volume spike + MACD cross"
    }

  Valid actions: BUY, SELL, STRONG_BUY, STRONG_SELL, CLOSE, NEUTRAL

Notes:
  - {{close}}, {{volume}}, {{plot("RSI")}} are TradingView placeholders
    that auto-fill with real values when the alert fires.
  - The "secret" field must match TV_WEBHOOK_SECRET env var.
  - Signals expire after signal_ttl_seconds (default 5 min).
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import TradingViewConfig

logger = logging.getLogger("celerity.tradingview")


# ─── Valid actions from TradingView ───
VALID_ACTIONS = {
    "BUY", "SELL", "STRONG_BUY", "STRONG_SELL",
    "CLOSE", "NEUTRAL", "HOLD",
}

# Map TV actions to internal score (-1.0 to +1.0)
ACTION_SCORES = {
    "STRONG_BUY":  0.9,
    "BUY":         0.6,
    "NEUTRAL":     0.0,
    "HOLD":        0.0,
    "SELL":       -0.6,
    "STRONG_SELL": -0.9,
    "CLOSE":      -0.5,
}

# Default confidence if not provided in alert
DEFAULT_CONFIDENCE = {
    "STRONG_BUY":  0.85,
    "BUY":         0.65,
    "NEUTRAL":     0.3,
    "HOLD":        0.3,
    "SELL":        0.65,
    "STRONG_SELL": 0.85,
    "CLOSE":       0.7,
}


@dataclass
class TVSignal:
    """A single signal received from TradingView."""
    symbol: str
    action: str           # BUY, SELL, STRONG_BUY, etc.
    score: float          # -1.0 to +1.0
    price: float          # Price at alert time
    confidence: float     # 0.0 to 1.0
    indicator: str        # Which indicator triggered it
    timeframe: str        # Chart timeframe
    message: str          # Human-readable description
    timestamp: float      # Unix timestamp of reception
    received_at: str      # ISO format
    # Optional extra data from TV
    rsi: Optional[float] = None
    volume: Optional[float] = None
    raw_payload: Dict = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        """How old this signal is in seconds."""
        return time.time() - self.timestamp

    @property
    def is_bullish(self) -> bool:
        return self.score > 0.1

    @property
    def is_bearish(self) -> bool:
        return self.score < -0.1


class TradingViewReceiver:
    """
    Receives, validates, and manages TradingView webhook signals.
    Thread-safe signal store with automatic expiry.
    """

    def __init__(self, config: TradingViewConfig):
        self.config = config
        self._signals: Dict[str, List[TVSignal]] = {}  # symbol -> signals
        self._lock = threading.Lock()
        self._stats = {
            "total_received": 0,
            "total_rejected": 0,
            "total_expired": 0,
            "last_received_at": None,
        }

    def validate_and_store(self, payload: Dict) -> Dict:
        """
        Validate an incoming webhook payload and store the signal.

        Returns dict with status and details.
        """
        # ─── Authentication ───
        secret = payload.get("secret", "")
        if secret != self.config.webhook_secret:
            self._stats["total_rejected"] += 1
            logger.warning(f"TV webhook rejected: invalid secret")
            return {"status": "error", "message": "Invalid secret token"}

        # ─── Required fields ───
        symbol = payload.get("symbol", "").upper().strip()
        action = payload.get("action", "").upper().strip()

        if not symbol:
            self._stats["total_rejected"] += 1
            return {"status": "error", "message": "Missing 'symbol' field"}

        if action not in VALID_ACTIONS:
            self._stats["total_rejected"] += 1
            return {
                "status": "error",
                "message": f"Invalid action '{action}'. Valid: {', '.join(sorted(VALID_ACTIONS))}",
            }

        # ─── Parse optional fields ───
        try:
            price = float(payload.get("price", 0))
        except (ValueError, TypeError):
            price = 0.0

        try:
            confidence = float(payload.get("confidence", DEFAULT_CONFIDENCE.get(action, 0.5)))
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = DEFAULT_CONFIDENCE.get(action, 0.5)

        indicator = str(payload.get("indicator", "TradingView Alert"))
        timeframe = str(payload.get("timeframe", "unknown"))
        message = str(payload.get("message", f"{action} signal from TradingView"))

        # Optional indicator values
        rsi = None
        volume = None
        try:
            if "rsi" in payload:
                rsi = float(payload["rsi"])
        except (ValueError, TypeError):
            pass
        try:
            if "volume" in payload:
                volume = float(payload["volume"])
        except (ValueError, TypeError):
            pass

        # ─── Build signal ───
        score = ACTION_SCORES.get(action, 0.0)

        # Scale score by confidence (high confidence = full score, low = dampened)
        score = score * confidence

        now = time.time()
        signal = TVSignal(
            symbol=symbol,
            action=action,
            score=round(score, 3),
            price=price,
            confidence=round(confidence, 3),
            indicator=indicator,
            timeframe=timeframe,
            message=message,
            timestamp=now,
            received_at=datetime.now(timezone.utc).isoformat(),
            rsi=rsi,
            volume=volume,
            raw_payload=payload,
        )

        # ─── Store (thread-safe) ───
        with self._lock:
            if symbol not in self._signals:
                self._signals[symbol] = []

            self._signals[symbol].append(signal)

            # Trim old signals
            max_signals = self.config.max_signals_per_symbol
            if len(self._signals[symbol]) > max_signals:
                self._signals[symbol] = self._signals[symbol][-max_signals:]

            self._stats["total_received"] += 1
            self._stats["last_received_at"] = signal.received_at

        logger.info(
            f"TV signal received: {symbol} {action} (score: {score:+.2f}, "
            f"conf: {confidence:.0%}, indicator: {indicator}, tf: {timeframe})"
        )

        return {
            "status": "ok",
            "signal": {
                "symbol": symbol,
                "action": action,
                "score": signal.score,
                "confidence": signal.confidence,
                "indicator": indicator,
                "message": message,
            },
        }

    def get_latest_signal(self, symbol: str) -> Optional[TVSignal]:
        """
        Get the most recent VALID (non-expired) signal for a symbol.
        Returns None if no valid signal exists.
        """
        with self._lock:
            signals = self._signals.get(symbol, [])
            if not signals:
                return None

            # Find most recent non-expired signal
            now = time.time()
            ttl = self.config.signal_ttl_seconds

            for signal in reversed(signals):
                if now - signal.timestamp <= ttl:
                    return signal

            return None

    def get_consensus(self, symbol: str) -> Dict:
        """
        Analyze all recent (non-expired) signals for a symbol
        and produce a consensus view.

        This is the main interface used by strategy.py.
        Returns a dict with aggregated score, confidence, and details.
        """
        with self._lock:
            all_signals = self._signals.get(symbol, [])

        if not all_signals:
            return {
                "score": 0.0,
                "confidence": 0.0,
                "signal": "NEUTRAL",
                "active_signals": 0,
                "details": "No TradingView signals",
                "latest": None,
            }

        # Filter to non-expired signals
        now = time.time()
        ttl = self.config.signal_ttl_seconds
        active = [s for s in all_signals if now - s.timestamp <= ttl]

        if not active:
            return {
                "score": 0.0,
                "confidence": 0.0,
                "signal": "NEUTRAL",
                "active_signals": 0,
                "details": "TV signals expired",
                "latest": None,
            }

        # ─── Weighted average: newer signals weigh more ───
        total_weight = 0.0
        weighted_score = 0.0
        weighted_confidence = 0.0

        for sig in active:
            # Recency weight: 1.0 for brand new, decays to 0.3 at expiry
            age_ratio = sig.age_seconds / ttl
            recency_weight = max(0.3, 1.0 - (age_ratio * 0.7))

            w = recency_weight * sig.confidence
            weighted_score += sig.score * w
            weighted_confidence += sig.confidence * w
            total_weight += w

        if total_weight > 0:
            consensus_score = weighted_score / total_weight
            consensus_confidence = weighted_confidence / total_weight
        else:
            consensus_score = 0.0
            consensus_confidence = 0.0

        # Clamp
        consensus_score = max(-1.0, min(1.0, consensus_score))
        consensus_confidence = max(0.0, min(1.0, consensus_confidence))

        # Determine signal label
        if consensus_score > 0.3:
            signal_label = "BUY"
        elif consensus_score < -0.3:
            signal_label = "SELL"
        else:
            signal_label = "NEUTRAL"

        latest = active[-1]

        # Build human-readable summary
        indicators_used = list(set(s.indicator for s in active))
        timeframes_used = list(set(s.timeframe for s in active if s.timeframe != "unknown"))

        details_parts = [f"TV: {signal_label} ({len(active)} signals"]
        if indicators_used:
            details_parts.append(f"via {', '.join(indicators_used[:3])}")
        if timeframes_used:
            details_parts.append(f"tf: {', '.join(timeframes_used[:3])}")
        details_parts[-1] += ")"
        details = " ".join(details_parts)

        return {
            "score": round(consensus_score, 3),
            "confidence": round(consensus_confidence, 3),
            "signal": signal_label,
            "active_signals": len(active),
            "details": details,
            "latest": {
                "action": latest.action,
                "price": latest.price,
                "indicator": latest.indicator,
                "timeframe": latest.timeframe,
                "message": latest.message,
                "age_seconds": round(latest.age_seconds, 0),
                "rsi": latest.rsi,
                "volume": latest.volume,
            },
        }

    def get_all_signals(self, symbol: str, include_expired: bool = False) -> List[Dict]:
        """Get all signals for a symbol (for dashboard display)."""
        with self._lock:
            all_signals = self._signals.get(symbol, [])

        now = time.time()
        ttl = self.config.signal_ttl_seconds
        result = []

        for s in all_signals:
            is_expired = (now - s.timestamp) > ttl
            if is_expired and not include_expired:
                continue

            result.append({
                "action": s.action,
                "score": s.score,
                "price": s.price,
                "confidence": s.confidence,
                "indicator": s.indicator,
                "timeframe": s.timeframe,
                "message": s.message,
                "received_at": s.received_at,
                "age_seconds": round(now - s.timestamp, 0),
                "expired": is_expired,
                "rsi": s.rsi,
                "volume": s.volume,
            })

        return result

    def get_stats(self) -> Dict:
        """Get overall webhook stats."""
        with self._lock:
            total_active = sum(
                len([s for s in sigs if s.age_seconds <= self.config.signal_ttl_seconds])
                for sigs in self._signals.values()
            )
            symbols_with_signals = [
                sym for sym, sigs in self._signals.items()
                if any(s.age_seconds <= self.config.signal_ttl_seconds for s in sigs)
            ]

        return {
            "enabled": self.config.enabled,
            "total_received": self._stats["total_received"],
            "total_rejected": self._stats["total_rejected"],
            "active_signals": total_active,
            "symbols_with_signals": symbols_with_signals,
            "signal_ttl_seconds": self.config.signal_ttl_seconds,
            "strategy_weight": self.config.strategy_weight,
            "last_received_at": self._stats["last_received_at"],
        }

    def cleanup_expired(self):
        """Remove expired signals from memory."""
        now = time.time()
        ttl = self.config.signal_ttl_seconds
        cleaned = 0

        with self._lock:
            for symbol in list(self._signals.keys()):
                before = len(self._signals[symbol])
                self._signals[symbol] = [
                    s for s in self._signals[symbol]
                    if now - s.timestamp <= ttl * 2  # Keep 2x TTL for history
                ]
                cleaned += before - len(self._signals[symbol])

                if not self._signals[symbol]:
                    del self._signals[symbol]

        if cleaned > 0:
            self._stats["total_expired"] += cleaned
            logger.debug(f"TV cleanup: removed {cleaned} expired signals")
