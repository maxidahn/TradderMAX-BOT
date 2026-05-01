"""
Celerity Trader Bot - Adaptive Strategy Engine
================================================
Dynamically adjusts strategy parameters based on market conditions.

Detects market regime:
  - TRENDING:    Strong directional movement → wider EMAs, ride the trend
  - RANGING:     Sideways/choppy → tighter RSI bands, mean reversion
  - VOLATILE:    High volatility → wider stops, smaller positions
  - QUIET:       Low volatility → tighter stops, look for breakouts

Continuously optimizes parameters using a rolling performance window.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

from config import StrategyConfig

logger = logging.getLogger("celerity.adaptive")


class MarketRegime:
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    QUIET = "QUIET"


@dataclass
class RegimeAnalysis:
    """Current market regime analysis."""
    regime: str
    trend_strength: float   # 0 (no trend) to 1 (strong trend)
    volatility_level: float # Relative volatility (1.0 = normal)
    regime_confidence: float
    description: str
    trend_direction: str = "neutral"  # "up", "down", or "neutral"


@dataclass
class AdaptedParameters:
    """Strategy parameters adjusted for current market conditions."""
    ema_fast: int
    ema_slow: int
    rsi_oversold: float
    rsi_overbought: float
    stop_loss_pct: float
    take_profit_pct: float
    volume_threshold: float
    position_size_multiplier: float  # 0.5 = half size, 1.0 = normal, 1.5 = bigger
    regime: str
    adjustments_made: list
    trend_direction: str = "neutral"  # "up", "down", or "neutral"


class AdaptiveEngine:
    """Dynamically adjusts strategy parameters based on market conditions."""

    # Base parameters (from config)
    BASE_EMA_FAST = 9
    BASE_EMA_SLOW = 21
    BASE_RSI_OVERSOLD = 30.0
    BASE_RSI_OVERBOUGHT = 70.0
    BASE_STOP_LOSS = 2.0
    BASE_TAKE_PROFIT = 3.0
    BASE_VOLUME_THRESHOLD = 1.2

    def __init__(self, base_config: StrategyConfig):
        self.base = base_config
        self._history: Dict[str, list] = {}  # Track regime history per symbol

    def detect_regime(self, df: pd.DataFrame) -> RegimeAnalysis:
        """
        Detect current market regime from candle data.

        Uses ADX for trend strength and ATR for volatility.
        """
        if df is None or len(df) < 50:
            return RegimeAnalysis(
                regime=MarketRegime.QUIET,
                trend_strength=0.0,
                volatility_level=1.0,
                regime_confidence=0.0,
                description="Insufficient data for regime detection",
            )

        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        # ─── Trend Strength (simplified ADX) ───
        # Using directional movement
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ], axis=1).max(axis=1)

        atr14 = tr.rolling(14).mean()
        plus_di = 100 * (plus_dm.rolling(14).mean() / atr14)
        minus_di = 100 * (minus_dm.rolling(14).mean() / atr14)

        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.rolling(14).mean()
        current_adx = adx.iloc[-1] if not np.isnan(adx.iloc[-1]) else 20

        # Normalize trend strength to 0-1
        trend_strength = min(current_adx / 50, 1.0)

        # ─── Volatility Level ───
        returns = close.pct_change()
        current_vol = returns.rolling(10).std().iloc[-1]
        avg_vol = returns.rolling(50).std().iloc[-1]

        if avg_vol and avg_vol > 0 and not np.isnan(avg_vol):
            volatility_level = current_vol / avg_vol
        else:
            volatility_level = 1.0

        if np.isnan(volatility_level):
            volatility_level = 1.0

        # ─── Classify Regime ───
        # Determine trend direction from DI lines
        trend_dir = "up" if plus_di.iloc[-1] > minus_di.iloc[-1] else "down"

        if trend_strength > 0.5 and volatility_level > 1.3:
            regime = MarketRegime.VOLATILE
            description = f"High volatility trending market (ADX: {current_adx:.0f}, Vol: {volatility_level:.1f}x, dir: {trend_dir})"
            confidence = min(trend_strength, volatility_level / 2)
            direction = trend_dir
        elif trend_strength > 0.4:
            regime = MarketRegime.TRENDING
            description = f"Clear trend detected (ADX: {current_adx:.0f}, direction: {trend_dir})"
            confidence = trend_strength
            direction = trend_dir
        elif volatility_level < 0.7:
            regime = MarketRegime.QUIET
            description = f"Low volatility, potential breakout building (Vol: {volatility_level:.1f}x normal)"
            confidence = 1 - volatility_level
            direction = "neutral"
        else:
            regime = MarketRegime.RANGING
            description = f"Sideways/choppy market (ADX: {current_adx:.0f}, Vol: {volatility_level:.1f}x)"
            confidence = 1 - trend_strength
            direction = "neutral"

        return RegimeAnalysis(
            regime=regime,
            trend_strength=round(trend_strength, 3),
            volatility_level=round(volatility_level, 3),
            regime_confidence=round(min(confidence, 1.0), 3),
            description=description,
            trend_direction=direction,
        )

    def adapt_parameters(self, df: pd.DataFrame, symbol: str) -> AdaptedParameters:
        """
        Adapt strategy parameters based on detected market regime.

        Returns optimized parameters for current conditions.
        """
        regime = self.detect_regime(df)
        adjustments = []

        # Start with base values
        ema_fast = self.base.ema_fast
        ema_slow = self.base.ema_slow
        rsi_oversold = self.base.rsi_oversold
        rsi_overbought = self.base.rsi_overbought
        stop_loss = self.base.stop_loss_pct
        take_profit = self.base.take_profit_pct
        vol_threshold = self.base.volume_threshold
        size_mult = 1.0

        if regime.regime == MarketRegime.TRENDING:
            # Ride the trend: wider EMAs, wider TP, relaxed volume
            ema_fast = 12
            ema_slow = 26
            take_profit = 4.5
            stop_loss = 2.5
            vol_threshold = 1.0  # Don't require volume spike in trends
            size_mult = 1.2
            adjustments = [
                "Wider EMAs (12/26) to ride trend",
                "Increased TP to 4.5%",
                "Relaxed volume requirement",
                "Position size +20%",
            ]

        elif regime.regime == MarketRegime.RANGING:
            # Mean reversion: tighter RSI bands, quick TP
            rsi_oversold = 25
            rsi_overbought = 75
            take_profit = 2.0
            stop_loss = 1.5
            vol_threshold = 1.3
            size_mult = 0.8
            adjustments = [
                "Tighter RSI bands (25/75) for mean reversion",
                "Quick TP at 2%",
                "Tighter SL at 1.5%",
                "Position size -20%",
            ]

        elif regime.regime == MarketRegime.VOLATILE:
            # Protect capital: wider stops, smaller size
            ema_fast = 7
            ema_slow = 18
            stop_loss = 3.5
            take_profit = 5.0
            vol_threshold = 1.5
            size_mult = 0.5  # Half position size
            adjustments = [
                "Faster EMAs (7/18) for quick reaction",
                "Wide SL at 3.5% (avoid whipsaws)",
                "Big TP target at 5%",
                "Position size HALVED for protection",
            ]

        elif regime.regime == MarketRegime.QUIET:
            # Look for breakouts: tight params, require strong volume
            ema_fast = 8
            ema_slow = 20
            rsi_oversold = 35
            rsi_overbought = 65
            stop_loss = 1.5
            take_profit = 3.0
            vol_threshold = 1.8  # Need strong volume for breakout confirmation
            size_mult = 1.0
            adjustments = [
                "Tighter RSI (35/65) for breakout sensitivity",
                "High volume threshold (1.8x) for confirmation",
                "Tight SL at 1.5%",
            ]

        # Track regime history
        if symbol not in self._history:
            self._history[symbol] = []
        self._history[symbol].append(regime.regime)
        if len(self._history[symbol]) > 100:
            self._history[symbol] = self._history[symbol][-100:]

        logger.info(f"{symbol} regime: {regime.regime} | {regime.description}")

        return AdaptedParameters(
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            rsi_oversold=rsi_oversold,
            rsi_overbought=rsi_overbought,
            stop_loss_pct=stop_loss,
            take_profit_pct=take_profit,
            volume_threshold=vol_threshold,
            position_size_multiplier=size_mult,
            regime=regime.regime,
            adjustments_made=adjustments,
            trend_direction=regime.trend_direction,
        )

    def get_regime_summary(self, symbol: str, df: pd.DataFrame) -> Dict:
        """Get regime summary for dashboard."""
        regime = self.detect_regime(df)
        adapted = self.adapt_parameters(df, symbol)

        return {
            "regime": regime.regime,
            "trend_strength": regime.trend_strength,
            "volatility_level": regime.volatility_level,
            "confidence": regime.regime_confidence,
            "description": regime.description,
            "adapted_params": {
                "ema": f"{adapted.ema_fast}/{adapted.ema_slow}",
                "rsi_bands": f"{adapted.rsi_oversold}/{adapted.rsi_overbought}",
                "stop_loss": f"{adapted.stop_loss_pct}%",
                "take_profit": f"{adapted.take_profit_pct}%",
                "volume_threshold": f"{adapted.volume_threshold}x",
                "size_multiplier": f"{adapted.position_size_multiplier}x",
            },
            "adjustments": adapted.adjustments_made,
            "regime_history": self._history.get(symbol, [])[-20:],
        }
