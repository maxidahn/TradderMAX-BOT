"""
Cross-Contagion — Aprendizaje en tiempo real entre agentes
=============================================================
Bus de eventos en memoria. Cuando un agente cierra un trade, publica el evento
con (features de entrada, pnl). El OTRO agente, al decidir, consulta el bus:

  - Si encuentra eventos recientes del rival en setups SIMILARES (similitud
    coseno alta), aplica un boost a su confidence:
      • Rival ganó setup similar → +0.15 (confianza)
      • Rival perdió setup similar → -0.10

  - Decay: el peso del boost cae a 0 después de contagion_lookback_minutes.

Esto cierra el loop de aprendizaje en tiempo real: el conocimiento del otro
agente impacta INMEDIATAMENTE, sin esperar tournament ni replay buffer.
"""

import logging
import math
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("celerity.contagion")

# Mismas features que online_ml (deben ser consistentes para el coseno)
FEATURE_KEYS = [
    "rsi", "rsi_momentum", "ema_spread_pct",
    "adx", "atr_pct", "bb_position",
    "volume_ratio", "ret_1m", "ret_5m", "ret_15m",
    "funding_rate", "ls_ratio",
]


class _Event:
    __slots__ = ("agent", "symbol", "feature_vector", "pnl_pct", "profitable", "timestamp")

    def __init__(self, agent, symbol, features_dict, pnl_pct):
        self.agent           = agent
        self.symbol          = symbol
        self.feature_vector  = _to_vector(features_dict)
        self.pnl_pct         = pnl_pct
        self.profitable      = pnl_pct > 0
        self.timestamp       = time.time()


def _to_vector(features: dict) -> np.ndarray:
    v = np.zeros(len(FEATURE_KEYS))
    for i, k in enumerate(FEATURE_KEYS):
        x = features.get(k, 0.0) if features else 0.0
        if x is None or (isinstance(x, float) and math.isnan(x)):
            x = 0.0
        try:
            v[i] = float(x)
        except (TypeError, ValueError):
            v[i] = 0.0
    return v


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class ContagionBus:

    def __init__(self,
                  boost_winner: float = 0.15,
                  boost_loser: float = -0.10,
                  lookback_minutes: int = 60,
                  similarity_threshold: float = 0.75):
        self.boost_winner         = boost_winner
        self.boost_loser          = boost_loser
        self.lookback_seconds     = lookback_minutes * 60
        self.similarity_threshold = similarity_threshold
        # Deque por agente; solo eventos recientes
        self._events: deque = deque(maxlen=200)
        # Stats
        self._publish_count = 0
        self._query_count   = 0
        self._boost_count   = 0

    def publish(self, agent_name: str, symbol: str, features: dict, pnl_pct: float):
        """Llamado por orchestrator tras cada cierre. Si features está vacío, no se publica."""
        if not features:
            return
        self._events.append(_Event(agent_name, symbol, features, pnl_pct))
        self._publish_count += 1
        logger.debug(
            f"[Contagion] published: {agent_name}/{symbol} pnl={pnl_pct:+.2f}% "
            f"(total events: {len(self._events)})"
        )

    def boost_for(self, current_agent_name: str, current_features: dict) -> Tuple[float, str]:
        """
        Devuelve (boost, reason). Boost se suma directamente al confidence del agente.
        Solo considera eventos del rival (no de self), recientes y con setup similar.
        """
        self._query_count += 1
        if not current_features:
            return 0.0, ""

        current_vec = _to_vector(current_features)
        now = time.time()
        total_boost = 0.0
        matches: List[str] = []

        for evt in self._events:
            # Filtrar: rival, reciente, similar
            if evt.agent == current_agent_name:
                continue
            age = now - evt.timestamp
            if age > self.lookback_seconds:
                continue
            sim = _cosine_similarity(current_vec, evt.feature_vector)
            if sim < self.similarity_threshold:
                continue

            # Time-decay: peso lineal de 1.0 → 0.0 cuando age → lookback
            decay = max(0.0, 1.0 - age / self.lookback_seconds)
            if evt.profitable:
                contrib = self.boost_winner * sim * decay
            else:
                contrib = self.boost_loser * sim * decay
            total_boost += contrib
            matches.append(
                f"{evt.agent}/{evt.symbol} "
                f"({'+' if evt.profitable else ''}{evt.pnl_pct:.1f}%, sim={sim:.2f})"
            )

        if total_boost == 0.0:
            return 0.0, ""

        self._boost_count += 1
        # Cap el boost combinado a ±0.30 para evitar amplificación excesiva
        total_boost = max(-0.30, min(0.30, total_boost))
        reason = f"contagion ({len(matches)} matches): {', '.join(matches[:3])}"
        return round(total_boost, 3), reason

    def recent_events(self, limit: int = 20) -> List[dict]:
        """Para dashboard."""
        now = time.time()
        out = []
        for evt in list(self._events)[-limit:]:
            out.append({
                "agent":      evt.agent,
                "symbol":     evt.symbol,
                "pnl_pct":    round(evt.pnl_pct, 2),
                "profitable": evt.profitable,
                "age_sec":    int(now - evt.timestamp),
            })
        return out

    def get_status(self) -> dict:
        return {
            "active_events":     len([e for e in self._events
                                       if time.time() - e.timestamp <= self.lookback_seconds]),
            "publish_count":     self._publish_count,
            "query_count":       self._query_count,
            "boost_count":       self._boost_count,
            "boost_winner":      self.boost_winner,
            "boost_loser":       self.boost_loser,
            "lookback_minutes":  self.lookback_seconds // 60,
            "similarity_threshold": self.similarity_threshold,
            "recent_events":     self.recent_events(10),
        }
