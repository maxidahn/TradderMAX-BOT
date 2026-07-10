"""
Celerity Multi-Agent — Base Agent
==================================
Interfaz común para todos los agentes. Cada agente implementa `decide()` y
opcionalmente `extract_features()` (default: features técnicos estándar).

Convención:
  - decide() devuelve un AgentDecision con action ∈ {LONG, SHORT, FLAT}
  - confidence ∈ [0, 1] — el orchestrator descarta decisiones < params.min_confidence
  - features es un dict serializable que se guarda en el ReplayBuffer
  - reasoning es texto plano para logs y dashboard
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("celerity.agent")


class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"          # No actuar
    CLOSE = "CLOSE"        # Cerrar posición existente (señal de salida adelantada)


@dataclass
class AgentDecision:
    """Decisión de un agente para un símbolo en un tick determinado."""
    agent_name: str
    symbol: str
    action: Action
    confidence: float       # 0.0 – 1.0
    features: Dict          # Snapshot de features usados → guardado en replay
    reasoning: str          # Texto plano para log/dashboard
    sl_pct: Optional[float] = None
    tp_pct: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _safe(x):
    """Convierte numpy types a float Python (JSON-serializable)."""
    if isinstance(x, (np.floating, np.integer)):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def compute_common_features(df: pd.DataFrame) -> Dict:
    """
    Set de features comunes que todos los agentes usan como input.
    Calculados sobre velas ya cerradas (df.iloc[:-1]).
    """
    if df is None or len(df) < 30:
        return {}
    df = df.iloc[:-1].copy()  # exclude current incomplete candle
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    vol   = df["volume"].astype(float)

    # EMAs
    ema9  = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    # RSI 14
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=13, min_periods=14).mean()
    avg_loss = loss.ewm(com=13, min_periods=14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # ATR & ADX (simplified)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    plus_di  = 100 * (plus_dm.rolling(14).mean() / atr14)
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr14)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(14).mean()

    # Bollinger
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_position = (close.iloc[-1] - bb_mid.iloc[-1]) / (2 * bb_std.iloc[-1]) if bb_std.iloc[-1] > 0 else 0

    # Volume
    vol_ma = vol.rolling(20).mean()
    vol_ratio = vol.iloc[-1] / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1.0

    # Returns
    ret_1   = float(close.pct_change(1).iloc[-1]) if len(close) > 1 else 0.0
    ret_5   = float(close.pct_change(5).iloc[-1]) if len(close) > 5 else 0.0
    ret_15  = float(close.pct_change(15).iloc[-1]) if len(close) > 15 else 0.0

    # Trend direction
    trend_dir = "up" if (plus_di.iloc[-1] > minus_di.iloc[-1]) else "down"

    return {
        "price":      _safe(close.iloc[-1]),
        "ema9":       _safe(ema9.iloc[-1]),
        "ema21":      _safe(ema21.iloc[-1]),
        "ema50":      _safe(ema50.iloc[-1]),
        "ema_spread_pct": _safe((ema9.iloc[-1] - ema21.iloc[-1]) / ema21.iloc[-1] * 100) if ema21.iloc[-1] else 0,
        "rsi":        _safe(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0,
        "rsi_momentum": _safe(rsi.iloc[-1] - rsi.iloc[-4]) if len(rsi) >= 4 else 0.0,
        "atr":        _safe(atr14.iloc[-1]) if not pd.isna(atr14.iloc[-1]) else 0.0,
        "atr_pct":    _safe(atr14.iloc[-1] / close.iloc[-1] * 100) if not pd.isna(atr14.iloc[-1]) and close.iloc[-1] > 0 else 0.0,
        "adx":        _safe(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0.0,
        "plus_di":    _safe(plus_di.iloc[-1]) if not pd.isna(plus_di.iloc[-1]) else 0.0,
        "minus_di":   _safe(minus_di.iloc[-1]) if not pd.isna(minus_di.iloc[-1]) else 0.0,
        "trend_dir":  trend_dir,
        "bb_upper":   _safe(bb_upper.iloc[-1]) if not pd.isna(bb_upper.iloc[-1]) else 0.0,
        "bb_lower":   _safe(bb_lower.iloc[-1]) if not pd.isna(bb_lower.iloc[-1]) else 0.0,
        "bb_position": _safe(bb_position),
        "volume_ratio": _safe(vol_ratio),
        "ret_1m":     _safe(ret_1),
        "ret_5m":     _safe(ret_5),
        "ret_15m":    _safe(ret_15),
    }


class BaseAgent(ABC):
    """Clase base para todos los agentes."""

    def __init__(self, name: str, params, perpetuals_data=None, replay_buffer=None,
                  online_learner=None, contagion_bus=None,
                  online_ml_weight: float = 0.25,
                  online_ml_min_samples: int = 8):
        self.name = name
        self.params = params       # AgentParams instance (mutado por tournament)
        self.perp = perpetuals_data
        self.replay = replay_buffer
        # ── Aprendizaje acelerado (#2, #5) — inyectados por orchestrator ────
        self.online_learner = online_learner
        self.contagion_bus  = contagion_bus
        self.online_ml_weight = online_ml_weight
        self.online_ml_min_samples = online_ml_min_samples
        # Telemetría para dashboard
        self._last_boost = 0.0
        self._last_boost_reason = ""
        self._last_ml_score = 0.0

    @abstractmethod
    def decide(self, symbol: str, candles: pd.DataFrame) -> AgentDecision:
        """Devuelve una AgentDecision para el símbolo en este tick."""
        raise NotImplementedError

    def apply_learning_boost(self, base_confidence: float, features: dict) -> tuple:
        """
        Aplica:
          - Boost del OnlineLearner (#2): score del modelo entrenado tras cada trade
          - Boost del ContagionBus (#5): aprendizaje de los trades recientes del rival

        Devuelve (new_confidence, boost_details_str). Llamado por momentum_agent y
        reversion_agent justo antes de armar la AgentDecision final.
        """
        details = []
        new_conf = base_confidence

        # ── Online ML boost ──────────────────────────────────────────────────
        # FIX 2026-06-09 (auditoría P2): además del mínimo de samples, el
        # modelo debe demostrar accuracy >50% para opinar. Un modelo peor que
        # una moneda (como estaban: 30-37%) restaba señal en vez de sumarla.
        if self.online_learner and self.online_learner.n_samples >= self.online_ml_min_samples:
            min_acc = getattr(self, "online_ml_min_accuracy", 0.50)
            if self.online_learner.accuracy() > min_acc:
                ml_score = self.online_learner.predict_score(features)   # [-1, +1]
                self._last_ml_score = ml_score
                ml_boost = ml_score * self.online_ml_weight
                new_conf += ml_boost
                if abs(ml_boost) >= 0.02:
                    details.append(f"ml{ml_boost:+.2f}")
            else:
                self._last_ml_score = 0.0

        # ── Cross-contagion boost ────────────────────────────────────────────
        if self.contagion_bus:
            boost, reason = self.contagion_bus.boost_for(self.name, features)
            self._last_boost = boost
            self._last_boost_reason = reason
            if abs(boost) >= 0.02:
                new_conf += boost
                details.append(f"contagion{boost:+.2f}")

        new_conf = round(min(max(new_conf, 0.0), 1.0), 3)
        return new_conf, " · ".join(details) if details else ""

    def update_params(self, new_params):
        """Llamado por Tournament tras un crossover."""
        old = {k: getattr(self.params, k) for k in self.params.__dataclass_fields__}
        self.params = new_params
        new = {k: getattr(self.params, k) for k in self.params.__dataclass_fields__}
        diff = {k: (old[k], new[k]) for k in new if old[k] != new[k]}
        if diff:
            logger.info(f"[{self.name}] params updated by tournament: {diff}")

    def get_recent_performance(self, n: int = 20) -> Dict:
        """
        Lee del replay buffer las últimas N decisiones cerradas y devuelve win_rate
        y avg_pnl_pct. Útil para que el agente "se enfríe" cuando va mal.
        """
        if not self.replay:
            return {"trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0}
        recent = self.replay.recent_closed_for_agent(self.name, limit=n)
        if not recent:
            return {"trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0}
        wins = sum(1 for r in recent if r.get("pnl_usdt", 0) > 0)
        avg_pnl_pct = sum(r.get("pnl_pct", 0) for r in recent) / len(recent)
        return {
            "trades":      len(recent),
            "win_rate":    round(wins / len(recent) * 100.0, 1),
            "avg_pnl_pct": round(avg_pnl_pct, 3),
        }

    def confidence_multiplier_from_replay(self) -> float:
        """
        Lee del replay buffer compartido qué resultados tuvo el OTRO agente
        en setups similares (regimen + dirección) y ajusta la confianza propia.
        Cross-learning: si el otro perdió plata en un escenario parecido, bajamos.

        2026-05-19 FIX: si NO hay 5+ trades en NINGÚN agente, devuelve 1.0
        (luna de miel). Antes devolvía 0.875 castigando injustamente a agentes
        nuevos sin historial — eso bloqueaba setups válidos por bug de diseño.
        """
        if not self.replay:
            return 1.0
        # Pondera: 80% propio, 20% del rival
        own = self.get_recent_performance(20)
        rival = self.replay.recent_closed_excluding(self.name, limit=20)

        own_trades   = own.get("trades", 0) if own else 0
        rival_trades = len(rival) if rival else 0

        # ── HONEYMOON INDIVIDUAL: el agente propio sin historial → sin penalty ──
        # No es justo que un agente nuevo (Sniper con 2 trades) sea penalizado
        # porque el rival (Momentum con 5+) tuvo mal WR. Cada agente arranca limpio.
        if own_trades < 5:
            return 1.0

        own_wr = own["win_rate"] / 100.0

        # Si el rival tiene historial significativo, lo ponderamos al 20%.
        # Si NO, ignorámoslo y vamos solo con el WR propio.
        if rival_trades >= 5:
            rival_wr = sum(1 for r in rival if r.get("pnl_usdt", 0) > 0) / rival_trades
            combined_wr = own_wr * 0.80 + rival_wr * 0.20
        else:
            combined_wr = own_wr

        # Win rate → multiplicador en rango [0.55, 1.20]
        # 0% → 0.55 | 50% → 0.90 | 100% → 1.20
        mult = 0.55 + combined_wr * 0.65
        return round(max(0.55, min(1.20, mult)), 2)
