"""
Online ML por agente — Aprendizaje incremental tras cada trade
================================================================
Cada agente tiene su propio "OnlineLearner": un perceptron logístico
entrenado con SGD que actualiza sus pesos después de CADA trade cerrado.

A diferencia del ML del spot bot (que reentrena cada 20 candles), este
aprende del outcome real: ¿fue rentable este trade? Sí/No → fit.

Implementación deliberadamente liviana (numpy puro, no sklearn) para que
encaje con el stack del bot y persista en JSON.

Features estandarizadas (z-score running) → robustas a magnitudes.
Pesos persistidos en data/online_ml_{agent}.json — sobreviven reinicios.
"""

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("celerity.online_ml")

DATA_DIR = os.getenv("DATA_DIR", "data")

# Features que se usan como input. Coinciden con compute_common_features().
# Si una feature falta o es nan, se reemplaza por 0.
FEATURE_KEYS = [
    "rsi", "rsi_momentum",
    "ema_spread_pct",
    "adx", "atr_pct",
    "bb_position",
    "volume_ratio",
    "ret_1m", "ret_5m", "ret_15m",
    "funding_rate", "ls_ratio",
]

# Bias term al final


class RunningStats:
    """Welford's online algorithm para mean/std en streaming (z-score sin lookback)."""

    def __init__(self, n_features: int):
        self.n = 0
        self.mean = np.zeros(n_features)
        self.M2 = np.zeros(n_features)

    def update(self, x: np.ndarray):
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.M2 += delta * delta2

    def std(self) -> np.ndarray:
        if self.n < 2:
            return np.ones_like(self.mean)
        var = self.M2 / (self.n - 1)
        return np.sqrt(np.where(var > 1e-8, var, 1.0))

    def normalize(self, x: np.ndarray) -> np.ndarray:
        if self.n < 5:    # Sin suficientes muestras → no normalizar
            return x
        return (x - self.mean) / self.std()

    def to_dict(self) -> dict:
        return {"n": self.n, "mean": self.mean.tolist(), "M2": self.M2.tolist()}

    @classmethod
    def from_dict(cls, d: dict, n_features: int):
        obj = cls(n_features)
        obj.n = int(d.get("n", 0))
        obj.mean = np.array(d.get("mean", [0.0] * n_features))
        obj.M2 = np.array(d.get("M2", [0.0] * n_features))
        return obj


class OnlineLearner:
    """Perceptron logístico con SGD + L2 regularization. Persiste en JSON."""

    def __init__(self, agent_name: str, lr: float = 0.05, l2: float = 0.001):
        self.agent_name = agent_name
        self.lr = lr
        self.l2 = l2
        self.n_features = len(FEATURE_KEYS)
        # Inicializar pesos cerca de 0 (no random — más estable los primeros trades)
        self.w = np.zeros(self.n_features + 1)   # +1 para bias
        self.stats = RunningStats(self.n_features)
        self.n_samples = 0
        self.correct_predictions = 0
        self.total_predictions = 0
        self._load()

    @staticmethod
    def _sigmoid(z):
        # Clip para evitar overflow
        return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))

    def _vectorize(self, features: dict) -> np.ndarray:
        """Extrae solo las features conocidas, fill 0 si falta."""
        v = np.zeros(self.n_features)
        for i, k in enumerate(FEATURE_KEYS):
            x = features.get(k, 0.0)
            if x is None or (isinstance(x, float) and math.isnan(x)):
                x = 0.0
            try:
                v[i] = float(x)
            except (TypeError, ValueError):
                v[i] = 0.0
        return v

    def predict_proba(self, features: dict) -> float:
        """
        Devuelve probabilidad de que el setup sea rentable (0..1).
        Si todavía no hay suficientes samples, devuelve 0.5 (neutral).
        """
        if self.n_samples < 5:
            return 0.5
        v = self._vectorize(features)
        v_norm = self.stats.normalize(v)
        # Add bias
        v_full = np.concatenate([v_norm, [1.0]])
        z = float(np.dot(self.w, v_full))
        return self._sigmoid(z)

    def predict_score(self, features: dict) -> float:
        """
        Devuelve score firmado [-1, +1] (útil para sumar al confidence del agente).
        Score = 2 * (proba - 0.5) ∈ [-1, +1].
        """
        p = self.predict_proba(features)
        return 2.0 * (p - 0.5)

    def partial_fit(self, features: dict, profitable: bool, sample_weight: float = 1.0):
        """
        Aprende de UN trade cerrado.
        features: snapshot de features en el momento de entrada
        profitable: True si pnl_usdt > 0
        sample_weight: peso de la muestra (ej. trades con mayor magnitud pueden pesar más)
        """
        v = self._vectorize(features)
        # Update running stats ANTES de normalizar (para que la muestra entre al stream)
        self.stats.update(v)
        v_norm = self.stats.normalize(v)
        v_full = np.concatenate([v_norm, [1.0]])

        y = 1.0 if profitable else 0.0
        z = float(np.dot(self.w, v_full))
        p = self._sigmoid(z)

        # Track accuracy (de la PREDICCIÓN antes de actualizar)
        pred_class = 1 if p > 0.5 else 0
        if pred_class == int(y):
            self.correct_predictions += 1
        self.total_predictions += 1

        # SGD step con L2 regularization y sample_weight
        # gradient = (p - y) * v_full + l2 * w
        gradient = (p - y) * v_full + self.l2 * self.w
        self.w -= self.lr * sample_weight * gradient

        self.n_samples += 1
        self._save()
        logger.debug(
            f"[OnlineML/{self.agent_name}] fit: profitable={profitable} p={p:.3f} "
            f"samples={self.n_samples} acc={self.accuracy():.1%}"
        )

    def accuracy(self) -> float:
        if self.total_predictions == 0:
            return 0.0
        return self.correct_predictions / self.total_predictions

    def get_state(self) -> dict:
        """Para dashboard."""
        return {
            "agent":     self.agent_name,
            "samples":   self.n_samples,
            "accuracy":  round(self.accuracy(), 3),
            "weights":   {k: round(float(self.w[i]), 4) for i, k in enumerate(FEATURE_KEYS)},
            "bias":      round(float(self.w[-1]), 4),
            "top_features": self._top_features(3),
        }

    def _top_features(self, n: int = 3) -> List[dict]:
        """Top features by |weight| después de normalización (importancia relativa)."""
        importances = [(k, abs(self.w[i])) for i, k in enumerate(FEATURE_KEYS)]
        importances.sort(key=lambda x: x[1], reverse=True)
        return [{"feature": k, "weight": round(self.w[FEATURE_KEYS.index(k)], 3)} for k, _ in importances[:n]]

    # ─── Persistencia ────────────────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            path = os.path.join(DATA_DIR, f"online_ml_{self.agent_name}.json")
            with open(path, "w") as f:
                json.dump({
                    "agent_name":  self.agent_name,
                    "lr":          self.lr,
                    "l2":          self.l2,
                    "w":           self.w.tolist(),
                    "stats":       self.stats.to_dict(),
                    "n_samples":   self.n_samples,
                    "correct":     self.correct_predictions,
                    "total":       self.total_predictions,
                    "updated_at":  datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"[OnlineML/{self.agent_name}] save failed: {e}")

    def _load(self):
        path = os.path.join(DATA_DIR, f"online_ml_{self.agent_name}.json")
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                d = json.load(f)
            self.w = np.array(d.get("w", self.w.tolist()))
            self.stats = RunningStats.from_dict(d.get("stats", {}), self.n_features)
            self.n_samples = int(d.get("n_samples", 0))
            self.correct_predictions = int(d.get("correct", 0))
            self.total_predictions = int(d.get("total", 0))
            logger.info(f"[OnlineML/{self.agent_name}] restored {self.n_samples} samples, acc={self.accuracy():.1%}")
        except Exception as e:
            logger.warning(f"[OnlineML/{self.agent_name}] load failed: {e}")
