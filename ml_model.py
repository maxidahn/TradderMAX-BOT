"""
Celerity Trader Bot - Machine Learning Engine
===============================================
Gradient Boosted Decision Tree model that learns from historical
candle patterns to predict short-term price direction.

Features engineered from candle data:
  - Price returns (1, 3, 5, 10 candle lookback)
  - RSI, EMA ratios
  - Volume patterns
  - Candle body/wick ratios
  - Volatility measures

The model trains on recent data and continuously retrains
as new candles come in (online learning approach).
"""

import logging
import os
import pickle
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("celerity.ml")

# Prediction horizon: how many candles ahead to predict
PREDICTION_HORIZON = 6  # 6 x 5min = 30 minutes ahead
# Minimum candles needed to train
# CORREGIDO: 200 → 80 (con lookback=150, teníamos ~130 candles útiles; 200 nunca se alcanzaba)
MIN_TRAIN_SAMPLES = 80
# Retrain every N new candles
# CORREGIDO: 50 → 20 (reentrenamiento cada ~1.7h en vez de cada ~4h → más ágil)
RETRAIN_INTERVAL = 20


class FeatureEngineer:
    """Transforms raw candle data into ML features."""

    @staticmethod
    def create_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        Engineer features from OHLCV candle data.

        Returns DataFrame with feature columns.
        """
        feat = pd.DataFrame(index=df.index)

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        opn = df["open"].astype(float)
        volume = df["volume"].astype(float)

        # ─── Price Returns ───
        for period in [1, 3, 5, 10, 20]:
            feat[f"return_{period}"] = close.pct_change(period)

        # ─── Moving Averages Ratios ───
        ema9 = close.ewm(span=9, adjust=False).mean()
        ema21 = close.ewm(span=21, adjust=False).mean()
        sma50 = close.rolling(50).mean()

        feat["ema_ratio_9_21"] = ema9 / ema21 - 1
        feat["price_vs_ema9"] = close / ema9 - 1
        feat["price_vs_ema21"] = close / ema21 - 1
        feat["price_vs_sma50"] = close / sma50 - 1

        # ─── RSI ───
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=13, min_periods=14).mean()
        avg_loss = loss.ewm(com=13, min_periods=14).mean()
        rs = avg_gain / avg_loss
        feat["rsi"] = 100 - (100 / (1 + rs))
        feat["rsi_normalized"] = feat["rsi"] / 100 - 0.5

        # ─── MACD ───
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        feat["macd_hist"] = (macd - signal) / close  # Normalized

        # ─── Volatility ───
        feat["volatility_10"] = close.pct_change().rolling(10).std()
        feat["volatility_20"] = close.pct_change().rolling(20).std()
        feat["atr_ratio"] = (high - low) / close  # Average True Range simplified

        # ─── Volume Features ───
        vol_ma = volume.rolling(20).mean()
        feat["volume_ratio"] = volume / vol_ma
        feat["volume_change"] = volume.pct_change()
        feat["volume_trend"] = volume.rolling(5).mean() / vol_ma

        # ─── Candle Patterns ───
        body = abs(close - opn)
        total_range = high - low
        feat["body_ratio"] = body / total_range.replace(0, np.nan)
        feat["upper_wick"] = (high - pd.concat([close, opn], axis=1).max(axis=1)) / total_range.replace(0, np.nan)
        feat["lower_wick"] = (pd.concat([close, opn], axis=1).min(axis=1) - low) / total_range.replace(0, np.nan)
        feat["is_green"] = (close > opn).astype(float)

        # ─── Consecutive green/red candles ───
        green = (close > opn).astype(int)
        groups = (green != green.shift()).cumsum()
        feat["consecutive_direction"] = green.groupby(groups).cumcount() + 1
        feat["consecutive_direction"] = feat["consecutive_direction"] * green.map({1: 1, 0: -1})

        # ─── Bollinger Band position ───
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        feat["bb_position"] = (close - bb_mid) / (2 * bb_std).replace(0, np.nan)

        return feat

    @staticmethod
    def create_target(df: pd.DataFrame, horizon: int = PREDICTION_HORIZON) -> pd.Series:
        """
        Create target variable: future return direction.
        1 = price goes up, 0 = price goes down/flat.
        """
        close = df["close"].astype(float)
        future_return = close.shift(-horizon) / close - 1
        # Binary: 1 if positive return, 0 otherwise
        target = (future_return > 0).astype(int)
        return target


class MLPredictor:
    """
    Gradient Boosted Tree model for price direction prediction.
    Uses a simple ensemble of decision stumps (manual implementation
    to avoid sklearn dependency, keeping it lightweight).
    """

    def __init__(self, model_dir: str = "models"):
        self.model_dir = model_dir
        os.makedirs(model_dir, exist_ok=True)
        self.feature_engineer = FeatureEngineer()
        self.models: Dict[str, dict] = {}  # symbol -> model data
        self._candle_count: Dict[str, int] = {}

    def train(self, df: pd.DataFrame, symbol: str) -> Dict:
        """
        Train the model on historical candle data.

        Returns training metrics.
        """
        if len(df) < MIN_TRAIN_SAMPLES:
            return {"status": "insufficient_data", "samples": len(df)}

        try:
            features = self.feature_engineer.create_features(df)
            target = self.feature_engineer.create_target(df)

            # Align and drop NaN
            combined = pd.concat([features, target.rename("target")], axis=1).dropna()

            if len(combined) < 50:
                return {"status": "insufficient_clean_data", "samples": len(combined)}

            X = combined.drop("target", axis=1).values
            y = combined["target"].values
            feature_names = list(combined.drop("target", axis=1).columns)

            # Split: train on first 80%, validate on last 20%
            split = int(len(X) * 0.8)
            X_train, X_val = X[:split], X[split:]
            y_train, y_val = y[:split], y[split:]

            # Train simple gradient boosted stumps
            model = SimpleGBM(n_estimators=30, learning_rate=0.1, max_depth=3)
            model.fit(X_train, y_train)

            # Validate
            val_preds = model.predict(X_val)
            accuracy = np.mean(val_preds == y_val)

            # Feature importance
            importance = model.feature_importance()
            top_features = sorted(
                zip(feature_names, importance),
                key=lambda x: x[1],
                reverse=True,
            )[:5]

            self.models[symbol] = {
                "model": model,
                "feature_names": feature_names,
                "accuracy": accuracy,
                "train_samples": len(X_train),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            }
            self._candle_count[symbol] = 0

            # Save model
            model_path = os.path.join(self.model_dir, f"{symbol}_model.pkl")
            with open(model_path, "wb") as f:
                pickle.dump(self.models[symbol], f)

            logger.info(f"ML model trained for {symbol}: accuracy={accuracy:.1%}, samples={len(X_train)}")

            return {
                "status": "trained",
                "accuracy": round(accuracy, 3),
                "train_samples": len(X_train),
                "val_samples": len(X_val),
                "top_features": [(name, round(imp, 3)) for name, imp in top_features],
            }

        except Exception as e:
            logger.error(f"ML training failed for {symbol}: {e}")
            return {"status": "error", "error": str(e)}

    def predict(self, df: pd.DataFrame, symbol: str) -> Dict:
        """
        Predict price direction for the next period.

        Returns prediction with confidence.
        """
        if symbol not in self.models:
            # Try loading saved model
            model_path = os.path.join(self.model_dir, f"{symbol}_model.pkl")
            if os.path.exists(model_path):
                with open(model_path, "rb") as f:
                    self.models[symbol] = pickle.load(f)
            else:
                return {
                    "prediction": "NEUTRAL",
                    "confidence": 0.0,
                    "score": 0.0,
                    "status": "no_model",
                }

        try:
            model_data = self.models[symbol]
            features = self.feature_engineer.create_features(df)

            # Get last row (current state)
            latest = features.iloc[-1:].values

            if np.any(np.isnan(latest)):
                # Fill NaN with 0
                latest = np.nan_to_num(latest, 0)

            model = model_data["model"]
            pred_proba = model.predict_proba(latest)[0]
            prediction = 1 if pred_proba > 0.5 else 0
            confidence = abs(pred_proba - 0.5) * 2  # Scale to 0-1

            # Convert to score: -1.0 (strong sell) to +1.0 (strong buy)
            score = (pred_proba - 0.5) * 2

            # Track candles for retraining
            self._candle_count[symbol] = self._candle_count.get(symbol, 0) + 1
            needs_retrain = self._candle_count.get(symbol, 0) >= RETRAIN_INTERVAL

            return {
                "prediction": "BULLISH" if prediction == 1 else "BEARISH",
                "confidence": round(confidence, 3),
                "score": round(score, 3),
                "probability": round(pred_proba, 3),
                "model_accuracy": round(model_data["accuracy"], 3),
                "trained_at": model_data["trained_at"],
                "needs_retrain": needs_retrain,
                "status": "ok",
            }

        except Exception as e:
            logger.error(f"ML prediction failed for {symbol}: {e}")
            return {
                "prediction": "NEUTRAL",
                "confidence": 0.0,
                "score": 0.0,
                "status": "error",
                "error": str(e),
            }

    def should_retrain(self, symbol: str) -> bool:
        """Check if model needs retraining."""
        return self._candle_count.get(symbol, 0) >= RETRAIN_INTERVAL

    def get_feedback_confidence_multiplier(self, symbol: str) -> float:
        """
        Aprende del historial de trades reales (ml_feedback.json).

        Ajusta la confianza del modelo según el win rate reciente del par:
          - Win rate ≥ 60%  → multiplica hasta 1.20× (el modelo está funcionando bien)
          - Win rate ~ 40%  → multiplica 0.90× (rendimiento regular, ligera penalización)
          - Win rate ≤ 25%  → multiplica 0.55× (el modelo está fallando, reduce su peso)
          - < 5 trades      → neutral (sin datos suficientes)

        Retorna float entre 0.55 y 1.20.
        """
        try:
            import persistence
            feedback = persistence.load_ml_feedback()
            # Filtrar por símbolo
            sym_feedback = [f for f in feedback if f.get("symbol") == symbol]
            recent = sym_feedback[-20:]  # Últimos 20 trades del par

            if len(recent) < 5:
                return 1.0  # Sin datos suficientes — neutral

            win_rate = sum(1 for f in recent if f.get("profitable", False)) / len(recent)

            # Pesos más recientes tienen más influencia (recency weighting)
            weighted_wins = 0.0
            total_weight  = 0.0
            for i, fb in enumerate(recent):
                w = 1.0 + (i / len(recent)) * 0.5  # más reciente → mayor peso
                total_weight  += w
                if fb.get("profitable", False):
                    weighted_wins += w
            weighted_win_rate = weighted_wins / total_weight if total_weight > 0 else win_rate

            # Mapear win rate → multiplicador (0.55 – 1.20)
            # 0%  → 0.55 | 50% → 0.90 | 100% → 1.20
            multiplier = 0.55 + weighted_win_rate * 0.65
            multiplier = round(min(1.20, max(0.55, multiplier)), 2)

            if multiplier != 1.0:
                logger.debug(
                    f"ML feedback [{symbol}]: win_rate={weighted_win_rate:.0%} "
                    f"(n={len(recent)}) → confidence ×{multiplier}"
                )
            return multiplier

        except Exception as e:
            logger.debug(f"ML feedback multiplier error for {symbol}: {e}")
            return 1.0


# ─── Simple Gradient Boosted Model (no sklearn needed) ───

class DecisionStump:
    """Simple decision tree of limited depth."""

    def __init__(self, max_depth: int = 3):
        self.max_depth = max_depth
        self.tree = None

    def fit(self, X: np.ndarray, residuals: np.ndarray):
        """Fit a decision tree to residuals."""
        self.tree = self._build_tree(X, residuals, depth=0)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Predict using the tree."""
        return np.array([self._predict_one(x, self.tree) for x in X])

    def _build_tree(self, X, y, depth):
        """Recursively build tree."""
        if depth >= self.max_depth or len(y) < 10:
            return {"type": "leaf", "value": np.mean(y) if len(y) > 0 else 0}

        best_feature, best_threshold, best_score = None, None, float("inf")
        n_features = X.shape[1]

        # Sample features for speed
        feature_indices = np.random.choice(n_features, min(n_features, 10), replace=False)

        for feat_idx in feature_indices:
            values = X[:, feat_idx]
            thresholds = np.percentile(values[~np.isnan(values)], [25, 50, 75]) if len(values[~np.isnan(values)]) > 0 else []

            for threshold in thresholds:
                left_mask = values <= threshold
                right_mask = ~left_mask

                if left_mask.sum() < 5 or right_mask.sum() < 5:
                    continue

                left_var = np.var(y[left_mask]) * left_mask.sum()
                right_var = np.var(y[right_mask]) * right_mask.sum()
                score = left_var + right_var

                if score < best_score:
                    best_score = score
                    best_feature = feat_idx
                    best_threshold = threshold

        if best_feature is None:
            return {"type": "leaf", "value": np.mean(y)}

        mask = X[:, best_feature] <= best_threshold

        return {
            "type": "split",
            "feature": best_feature,
            "threshold": best_threshold,
            "left": self._build_tree(X[mask], y[mask], depth + 1),
            "right": self._build_tree(X[~mask], y[~mask], depth + 1),
        }

    def _predict_one(self, x, node):
        """Predict for a single sample."""
        if node["type"] == "leaf":
            return node["value"]

        if x[node["feature"]] <= node["threshold"]:
            return self._predict_one(x, node["left"])
        else:
            return self._predict_one(x, node["right"])


class SimpleGBM:
    """Simple Gradient Boosted Machine."""

    def __init__(self, n_estimators: int = 30, learning_rate: float = 0.1, max_depth: int = 3):
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.trees: List[DecisionStump] = []
        self.base_prediction = 0.0
        self._feature_splits: Dict[int, int] = {}

    def fit(self, X: np.ndarray, y: np.ndarray):
        """Train the model."""
        self.base_prediction = np.mean(y)
        predictions = np.full(len(y), self.base_prediction)
        self.trees = []
        self._feature_splits = {}

        for i in range(self.n_estimators):
            # Compute residuals
            residuals = y - self._sigmoid(predictions)

            # Fit tree to residuals
            tree = DecisionStump(max_depth=self.max_depth)
            tree.fit(X, residuals)
            self.trees.append(tree)

            # Update predictions
            update = tree.predict(X)
            predictions += self.learning_rate * update

            # Track feature usage
            self._count_features(tree.tree)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Predict probability of class 1."""
        predictions = np.full(len(X), self.base_prediction)
        for tree in self.trees:
            predictions += self.learning_rate * tree.predict(X)
        return self._sigmoid(predictions)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Predict class labels."""
        proba = self.predict_proba(X)
        return (proba > 0.5).astype(int)

    def feature_importance(self) -> np.ndarray:
        """Get feature importance scores."""
        if not self._feature_splits:
            return np.zeros(1)
        max_feat = max(self._feature_splits.keys()) + 1
        importance = np.zeros(max_feat)
        for feat_idx, count in self._feature_splits.items():
            importance[feat_idx] = count
        total = importance.sum()
        if total > 0:
            importance /= total
        return importance

    def _count_features(self, node):
        """Count feature usage in tree."""
        if node["type"] == "leaf":
            return
        feat = node["feature"]
        self._feature_splits[feat] = self._feature_splits.get(feat, 0) + 1
        self._count_features(node["left"])
        self._count_features(node["right"])

    @staticmethod
    def _sigmoid(x):
        """Sigmoid function."""
        return 1 / (1 + np.exp(-np.clip(x, -500, 500)))
