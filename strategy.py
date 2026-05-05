"""
Celerity Trader Bot - AI-Enhanced Strategy Engine
===================================================
Combines 5 intelligence layers for trading decisions:

  1. TECHNICAL ANALYSIS:  RSI + EMA Crossover + Volume (base signals)
  2. SENTIMENT ANALYSIS:  News headlines + market momentum + fear/greed
  3. MACHINE LEARNING:    Gradient boosted model trained on price patterns
  4. ADAPTIVE STRATEGY:   Auto-adjusts params based on market regime
  5. TRADINGVIEW SIGNALS: External alerts from TradingView webhooks

Final decision is a weighted vote across all 5 layers.
Each layer produces a score from -1 (strong sell) to +1 (strong buy).

Weight distribution (when TradingView is active):
  - Technical:    25%  (proven, reliable)
  - Sentiment:    12%  (early warning)
  - ML Model:     25%  (pattern recognition)
  - Adaptive:     23%  (regime awareness)
  - TradingView:  15%  (external confirmation from your charts)

When TradingView has no active signals, its weight is redistributed
proportionally to the other 4 layers (original weights).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum

import pandas as pd
import numpy as np

from config import TransactionCosts, TradingViewConfig
from sentiment import SentimentAnalyzer
from ml_model import MLPredictor
from claude_agent import ClaudeAgent
from adaptive import AdaptiveEngine
from tv_webhook import TradingViewReceiver

logger = logging.getLogger("celerity.strategy")


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class AIInsight:
    """Detailed insight from one AI layer."""
    source: str
    score: float  # -1 to +1
    signal: str   # BUY, SELL, HOLD
    confidence: float
    details: str


@dataclass
class AnalysisResult:
    """Result of full AI-enhanced strategy analysis."""
    signal: Signal
    symbol: str
    price: float
    rsi: float
    ema_fast: float
    ema_slow: float
    volume_ratio: float
    reason: str
    confidence: float

    # AI layers breakdown
    ai_score: float = 0.0  # Combined AI score (-1 to +1)
    insights: List[AIInsight] = field(default_factory=list)
    regime: str = ""
    sentiment_label: str = ""
    ml_prediction: str = ""
    tv_signal: str = ""  # TradingView signal if active

    # For consultation mode
    explanation: str = ""


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    vol_ma = volume.rolling(window=period).mean()
    return volume / vol_ma


class Strategy:
    """AI-Enhanced trading strategy combining 5 intelligence layers."""

    # Base weights for 4 layers (without TradingView)
    WEIGHT_TECHNICAL_BASE = 0.30
    WEIGHT_SENTIMENT_BASE = 0.15
    WEIGHT_ML_BASE = 0.30
    WEIGHT_ADAPTIVE_BASE = 0.25

    # Weights when TradingView is active (5 layers)
    WEIGHT_TECHNICAL_TV = 0.25
    WEIGHT_SENTIMENT_TV = 0.12
    WEIGHT_ML_TV = 0.25
    WEIGHT_ADAPTIVE_TV = 0.23
    # TV weight comes from config.tradingview.strategy_weight (default 0.15)

    def __init__(self, config, costs_config: TransactionCosts = None,
                 tv_config: TradingViewConfig = None,
                 tv_receiver: TradingViewReceiver = None,
                 app_config=None):
        self.config = config
        self.app_config = app_config  # Full AppConfig for risk_level access
        self.costs = costs_config or TransactionCosts()
        self.tv_config = tv_config or TradingViewConfig()
        self.tv_receiver = tv_receiver  # Injected from bot.py
        self.sentiment = SentimentAnalyzer()
        self.ml = MLPredictor()
        self.adaptive = AdaptiveEngine(config)
        self.claude = ClaudeAgent()
        self._ml_trained: Dict[str, bool] = {}

    def analyze(self, df: pd.DataFrame, symbol: str) -> AnalysisResult:
        """
        Full AI-enhanced analysis combining all 4 layers.
        """
        if df is None or len(df) < 30:
            return AnalysisResult(
                signal=Signal.HOLD, symbol=symbol, price=0, rsi=50,
                ema_fast=0, ema_slow=0, volume_ratio=0,
                reason="Insufficient data", confidence=0.0,
                explanation="Not enough candle data to analyze.",
            )

        # ─── Drop the last (incomplete) candle ──────────────────────────────
        # Binance get_klines always returns the current in-progress candle as
        # the last row.  Its close/volume change every second, so any signal
        # derived from it is unstable and often captures a transient spike.
        # We analyse only fully-closed candles; the live price for dashboard
        # display comes separately from the real-time ticker.
        df = df.iloc[:-1].copy()
        if len(df) < 30:
            return AnalysisResult(
                signal=Signal.HOLD, symbol=symbol, price=0, rsi=50,
                ema_fast=0, ema_slow=0, volume_ratio=0,
                reason="Insufficient closed candles", confidence=0.0,
                explanation="Not enough closed candle data to analyze.",
            )

        insights = []

        # ─── Layer 1: Adaptive Regime Detection ───
        adapted = self.adaptive.adapt_parameters(df, symbol)
        regime_insight = self._analyze_regime(adapted)
        insights.append(regime_insight)

        # ─── Layer 2: Technical Analysis (with adapted params) ───
        tech_insight, tech_data = self._analyze_technical(df, symbol, adapted)
        insights.append(tech_insight)

        # ─── Layer 3: Sentiment Analysis ───
        sent_insight = self._analyze_sentiment(symbol)
        insights.append(sent_insight)

        # ─── Layer 4: Machine Learning ───
        ml_insight = self._analyze_ml(df, symbol)
        insights.append(ml_insight)

        # ─── Layer 5: TradingView Signals ───
        tv_insight = self._analyze_tradingview(symbol)
        tv_active = (
            self.tv_config.enabled
            and self.tv_receiver is not None
            and tv_insight.confidence >= self.tv_config.min_confidence
        )
        if tv_active:
            insights.append(tv_insight)

        # ─── Weighted Decision (dynamic weights) ───
        # If ML model is untrained (no closed trades yet), its weight is
        # redistributed proportionally to the active layers so the score
        # range stays 0–1 and thresholds work as expected.
        ml_untrained = (abs(ml_insight.score) < 0.001 and ml_insight.confidence < 0.1)

        if tv_active:
            tv_weight = self.tv_config.strategy_weight
            if ml_untrained:
                active_w = self.WEIGHT_TECHNICAL_TV + self.WEIGHT_SENTIMENT_TV + self.WEIGHT_ADAPTIVE_TV + tv_weight
                boost = 1.0 + self.WEIGHT_ML_TV / active_w if active_w > 0 else 1.0
                combined_score = (
                    tech_insight.score   * self.WEIGHT_TECHNICAL_TV  * boost +
                    sent_insight.score   * self.WEIGHT_SENTIMENT_TV  * boost +
                    regime_insight.score * self.WEIGHT_ADAPTIVE_TV   * boost +
                    tv_insight.score     * tv_weight                 * boost
                )
            else:
                combined_score = (
                    tech_insight.score   * self.WEIGHT_TECHNICAL_TV  +
                    sent_insight.score   * self.WEIGHT_SENTIMENT_TV  +
                    ml_insight.score     * self.WEIGHT_ML_TV         +
                    regime_insight.score * self.WEIGHT_ADAPTIVE_TV   +
                    tv_insight.score     * tv_weight
                )
        else:
            if ml_untrained:
                active_w = self.WEIGHT_TECHNICAL_BASE + self.WEIGHT_SENTIMENT_BASE + self.WEIGHT_ADAPTIVE_BASE
                boost = 1.0 + self.WEIGHT_ML_BASE / active_w if active_w > 0 else 1.0
                combined_score = (
                    tech_insight.score   * self.WEIGHT_TECHNICAL_BASE  * boost +
                    sent_insight.score   * self.WEIGHT_SENTIMENT_BASE  * boost +
                    regime_insight.score * self.WEIGHT_ADAPTIVE_BASE   * boost
                )
            else:
                combined_score = (
                    tech_insight.score   * self.WEIGHT_TECHNICAL_BASE  +
                    sent_insight.score   * self.WEIGHT_SENTIMENT_BASE  +
                    ml_insight.score     * self.WEIGHT_ML_BASE         +
                    regime_insight.score * self.WEIGHT_ADAPTIVE_BASE
                )

        # ─── Layer 6: Claude Sonnet Agent ───
        claude_result = self.claude.analyze(
            symbol=symbol,
            price=tech_data["price"],
            rsi=tech_data["rsi"],
            ema_fast=tech_data["ema_fast"],
            ema_slow=tech_data["ema_slow"],
            volume_ratio=tech_data["volume_ratio_completed"],
            regime=adapted.regime,
            base_score=combined_score,
            layer_scores={
                "Adaptive":   round(regime_insight.score, 3),
                "Technical":  round(tech_insight.score, 3),
                "Sentiment":  round(sent_insight.score, 3),
                "ML Model":   round(ml_insight.score, 3),
            },
        )
        if claude_result["score_adjustment"] != 0.0:
            combined_score += claude_result["score_adjustment"] * 0.20
            claude_insight = AIInsight(
                source="Claude",
                score=round(claude_result["score_adjustment"], 3),
                signal=claude_result["recommendation"],
                confidence=claude_result["confidence"],
                details=f"Claude: {claude_result['recommendation']} — {claude_result['reasoning']}",
            )
            insights.append(claude_insight)

        # Determine final signal — threshold from risk level only
        # (costs are already reflected in SL/TP levels; adding them here double-penalizes)
        cost_drag = self.costs.round_trip_fee_pct + self.costs.slippage_base_pct  # kept for cost display

        # Apply risk level parameters
        risk_params = self.app_config.get_risk_params() if self.app_config else {}
        base_threshold = risk_params.get("signal_threshold", 0.25)
        adjusted_threshold = base_threshold  # no extra cost penalty

        # ── Override SL/TP from risk level (risk slider is the source of truth) ──
        if risk_params:
            adapted.stop_loss_pct   = risk_params.get("stop_loss_pct",   adapted.stop_loss_pct)
            adapted.take_profit_pct = risk_params.get("take_profit_pct", adapted.take_profit_pct)

        # Calculate overall confidence first (needed for min_confidence check)
        confidences = [i.confidence for i in insights if i.confidence > 0]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 0
        signs = [1 if i.score > 0.1 else (-1 if i.score < -0.1 else 0) for i in insights]
        agreement = abs(sum(signs)) / len(signs)
        final_confidence = min(avg_confidence * (1 + agreement * 0.3), 1.0)

        min_conf = risk_params.get("min_confidence", 0.30)

        # Multiplicador de umbral SELL escalado por nivel de riesgo (0.75x bajo → 1.0x alto)
        # Nivel bajo = salida más ágil ante señal negativa moderada
        # Nivel alto = espera más convicción antes de salir (deja correr las ganancias)
        sell_mult = risk_params.get("sell_threshold_mult", 0.85)

        # Block volatile regime entries at low risk levels
        if not risk_params.get("trade_volatile", True) and adapted.regime == "VOLATILE":
            final_signal = Signal.HOLD
        # Block ranging market entries unless risk is high enough
        elif not risk_params.get("trade_ranging", True) and adapted.regime == "RANGING":
            final_signal = Signal.HOLD
        # Confidence gate — signal is real only if AI layers agree sufficiently
        elif final_confidence < min_conf:
            final_signal = Signal.HOLD
        elif combined_score > adjusted_threshold:
            # ── Filtro de tendencia bajista confirmada ───────────────────────
            # Nunca abrir compras cuando el régimen propio es TRENDING con dirección DOWN
            # (el filtro de correlación BTC en bot.py añade una capa adicional)
            if adapted.regime == "TRENDING" and getattr(adapted, "trend_direction", "neutral") == "down":
                final_signal = Signal.HOLD
            else:
                final_signal = Signal.BUY
        elif combined_score < -(adjusted_threshold * sell_mult):
            # Umbral SELL dinámico: sell_mult × threshold de compra
            # Más ágil y escalado por nivel de riesgo vs el anterior 0.60× fijo
            final_signal = Signal.SELL
        else:
            final_signal = Signal.HOLD

        # Build reasons
        active_reasons = [i.details for i in insights if abs(i.score) > 0.1]
        reason = " | ".join(active_reasons) if active_reasons else "No clear consensus"

        # Build human-readable explanation for consultation mode
        explanation = self._build_explanation(
            symbol, final_signal, combined_score, insights, adapted, tech_data
        )

        return AnalysisResult(
            signal=final_signal,
            symbol=symbol,
            price=tech_data["price"],
            rsi=tech_data["rsi"],
            ema_fast=tech_data["ema_fast"],
            ema_slow=tech_data["ema_slow"],
            volume_ratio=tech_data["volume_ratio_completed"],  # completed candles → accurate display
            reason=reason,
            confidence=round(final_confidence, 2),
            ai_score=round(combined_score, 3),
            insights=insights,
            regime=adapted.regime,
            sentiment_label=sent_insight.details.split(":")[0] if ":" in sent_insight.details else "",
            ml_prediction=ml_insight.signal,
            tv_signal=tv_insight.signal if tv_active else "",
            explanation=explanation,
        )

    def _analyze_technical(self, df, symbol, adapted) -> tuple:
        """Layer 1: Technical analysis with adapted parameters."""
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        ema_fast = calculate_ema(close, adapted.ema_fast)
        ema_slow = calculate_ema(close, adapted.ema_slow)
        rsi = calculate_rsi(close, self.config.rsi_period)
        vol_ratio = calculate_volume_ratio(volume, self.config.volume_ma_period)

        current_price = close.iloc[-1]
        current_rsi = rsi.iloc[-1] if not np.isnan(rsi.iloc[-1]) else 50.0
        current_ema_fast = ema_fast.iloc[-1]
        current_ema_slow = ema_slow.iloc[-1]
        current_vol_ratio = vol_ratio.iloc[-1] if not np.isnan(vol_ratio.iloc[-1]) else 1.0

        prev_ema_fast = ema_fast.iloc[-2]
        prev_ema_slow = ema_slow.iloc[-2]

        bullish_cross = (prev_ema_fast <= prev_ema_slow) and (current_ema_fast > current_ema_slow)
        bearish_cross = (prev_ema_fast >= prev_ema_slow) and (current_ema_fast < current_ema_slow)
        ema_bullish = current_ema_fast > current_ema_slow
        volume_confirmed = current_vol_ratio >= adapted.volume_threshold

        # ─── EMA spread: how far apart are the two EMAs (trend momentum) ───
        ema_spread_pct = ((current_ema_fast - current_ema_slow) / current_ema_slow) * 100

        # ─── RSI momentum: is RSI rising or falling? ───
        rsi_prev3 = rsi.iloc[-4:-1].mean() if len(rsi) >= 4 else rsi.iloc[-2]
        rsi_momentum = current_rsi - rsi_prev3

        # ─── Price vs both EMAs ───
        price_above_fast = current_price > current_ema_fast
        price_above_slow = current_price > current_ema_slow

        # Score calculation
        score = 0.0
        reasons = []

        # Pre-compute extension % now (used in cross quality check below)
        price_ext_for_cross = (current_price - current_ema_fast) / current_ema_fast * 100

        # 1. EMA crossover (strongest signal)
        if bullish_cross:
            # If price is already extended at the moment of cross, reduce the bonus —
            # we're likely entering right at the top of the spike that caused the cross.
            # A fresh cross with price > 0.8% above EMA is a "late entry" risk.
            if price_ext_for_cross > 0.8:
                score += 0.20
                reasons.append(f"EMA bullish cross (precio extendido +{price_ext_for_cross:.1f}%)")
            else:
                score += 0.50
                reasons.append("EMA bullish cross")
        elif bearish_cross:
            score -= 0.50
            reasons.append("EMA bearish cross")
        else:
            # 2. EMA alignment + spread (established trend)
            if ema_bullish:
                score += 0.15
                # Extra score proportional to how far fast > slow (max +0.20 at 1% spread)
                spread_bonus = min(ema_spread_pct / 1.0 * 0.20, 0.20)
                score += spread_bonus
                if ema_spread_pct > 0.3:
                    reasons.append(f"EMA spread +{ema_spread_pct:.2f}%")
            else:
                score -= 0.15
                spread_penalty = max(ema_spread_pct / 1.0 * 0.20, -0.20)
                score += spread_penalty  # negative when fast < slow

        # 3. Price vs EMA alignment
        if price_above_fast and price_above_slow:
            score += 0.08
        elif not price_above_fast and not price_above_slow:
            score -= 0.08

        # 4. RSI extreme zones
        if current_rsi < adapted.rsi_oversold:
            score += 0.30
            reasons.append(f"RSI oversold ({current_rsi:.0f})")
        elif current_rsi > adapted.rsi_overbought:
            score -= 0.30
            reasons.append(f"RSI overbought ({current_rsi:.0f})")
        else:
            # 5. RSI momentum (trending RSI adds signal conviction)
            if abs(rsi_momentum) > 3:
                mom_score = min(max(rsi_momentum / 25.0 * 0.15, -0.15), 0.15)
                score += mom_score
                if abs(rsi_momentum) > 6:
                    reasons.append(f"RSI {'rising' if rsi_momentum > 0 else 'falling'} ({rsi_momentum:+.1f})")

        # 6. Volume multiplier / penalty
        if current_vol_ratio < 0.30:
            # Hard penalty for very low volume — signal is unreliable
            score *= 0.50
            reasons.append(f"Vol muy baja {current_vol_ratio:.2f}x")
        elif volume_confirmed:
            score *= 1.25
            reasons.append(f"Vol {current_vol_ratio:.1f}x")

        # 7. Pullback-to-EMA quality filter
        # Ideal entry: price near the fast EMA (pullback to support after crossover).
        # Penaliza entrar cuando el precio ya se extendió demasiado arriba.
        price_ext_pct = (current_price - current_ema_fast) / current_ema_fast * 100
        if ema_bullish:
            if price_ext_pct > 1.5:
                # Price too far above EMA — chasing a move, bad entry risk
                score -= 0.25
                reasons.append(f"Precio extendido +{price_ext_pct:.1f}% sobre EMA")
            elif 0.0 <= price_ext_pct <= 0.5:
                # Price near EMA — pullback to support, ideal entry zone
                score += 0.15
                reasons.append(f"Pullback a EMA ({price_ext_pct:.2f}%)")
        elif not ema_bullish:
            if price_ext_pct < -1.5:
                # Price too far below EMA — oversold bounce chasing
                score += 0.25  # This is bearish EMA but deep oversold
                reasons.append(f"Precio extendido {price_ext_pct:.1f}% bajo EMA")

        # 8. RSI trend direction for bullish signals
        # Don't buy when RSI is falling — wait for RSI to turn upward
        if score > 0.10 and rsi_momentum < -3:
            score *= 0.65
            reasons.append(f"RSI cayendo ({rsi_momentum:+.1f})")

        score = max(-1, min(1, score))

        # Use avg of last 3 COMPLETED candles for volume ratio passed to Claude Agent
        # (current candle may be partially formed → artificially low volume)
        completed_vol_ratio = float(vol_ratio.iloc[-4:-1].mean()) if len(vol_ratio) >= 4 else current_vol_ratio
        completed_vol_ratio = round(completed_vol_ratio, 2)

        tech_data = {
            "price": current_price,
            "rsi": round(current_rsi, 1),
            "ema_fast": round(current_ema_fast, 2),
            "ema_slow": round(current_ema_slow, 2),
            "volume_ratio": current_vol_ratio,          # for technical scoring
            "volume_ratio_completed": completed_vol_ratio,  # for Claude Agent
        }

        detail = " + ".join(reasons) if reasons else "Neutral technicals"

        insight = AIInsight(
            source="Technical",
            score=round(score, 3),
            signal="BUY" if score > 0.2 else ("SELL" if score < -0.2 else "HOLD"),
            confidence=min(abs(score), 1.0),
            details=f"TA: {detail}",
        )

        return insight, tech_data

    def _analyze_sentiment(self, symbol: str) -> AIInsight:
        """Layer 2: Sentiment analysis."""
        try:
            result = self.sentiment.analyze(symbol)
            return AIInsight(
                source="Sentiment",
                score=result.score,
                signal="BUY" if result.score > 0.15 else ("SELL" if result.score < -0.15 else "HOLD"),
                confidence=result.confidence,
                details=f"{result.label}: score {result.score:+.2f}",
            )
        except Exception as e:
            logger.debug(f"Sentiment analysis failed: {e}")
            return AIInsight(
                source="Sentiment", score=0.0, signal="HOLD",
                confidence=0.0, details="Sentiment unavailable",
            )

    def get_fear_greed(self) -> dict:
        """Return the cached Fear & Greed Index data (no extra API call if cached)."""
        try:
            score, value, label = self.sentiment._fear_greed_index()
            return {"value": value, "label": label, "score": round(score, 3)}
        except Exception:
            return {"value": 0, "label": "Unavailable", "score": 0.0}

    def _analyze_ml(self, df: pd.DataFrame, symbol: str) -> AIInsight:
        """Layer 3: Machine Learning prediction con aprendizaje de resultados reales."""
        try:
            # Umbral reducido a 80 muestras (corrige bug: antes nunca entrenaba con 100 candles)
            MIN_TRAIN = 80
            # Train if not trained yet
            if not self._ml_trained.get(symbol) and len(df) >= MIN_TRAIN:
                metrics = self.ml.train(df, symbol)
                if metrics.get("status") == "trained":
                    self._ml_trained[symbol] = True
                    logger.info(f"ML model trained for {symbol}: accuracy={metrics['accuracy']:.1%}")

            # Auto-retrain if needed (cada 20 candles en vez de 50 → más ágil)
            if self.ml.should_retrain(symbol) and len(df) >= MIN_TRAIN:
                self.ml.train(df, symbol)
                self.ml._candle_count[symbol] = 0  # reset counter

            # Predict
            pred = self.ml.predict(df, symbol)

            if pred["status"] == "ok":
                # ── Aprendizaje de resultados reales ──────────────────────────
                # Ajusta la confianza del ML según el win rate reciente del par
                feedback_mult = self.ml.get_feedback_confidence_multiplier(symbol)
                # Cap ML score at ±0.30 — evita que el ML domine el score combinado
                # (sin cap llegaba a ±0.50, aportando el 87% del umbral por sí solo)
                raw_score = pred["score"] * feedback_mult
                adj_score = round(max(-0.30, min(0.30, raw_score)), 3)
                adj_conf  = round(min(pred["confidence"] * feedback_mult, 1.0), 3)
                feedback_tag = f" feedback:{feedback_mult:.2f}x" if feedback_mult != 1.0 else ""
                return AIInsight(
                    source="ML Model",
                    score=adj_score,
                    signal=pred["prediction"],
                    confidence=adj_conf,
                    details=f"ML: {pred['prediction']} ({pred['probability']:.0%} prob, acc:{pred['model_accuracy']:.0%}{feedback_tag})",
                )
        except Exception as e:
            logger.debug(f"ML prediction failed: {e}")

        return AIInsight(
            source="ML Model", score=0.0, signal="NEUTRAL",
            confidence=0.0, details="ML model training...",
        )

    def _analyze_tradingview(self, symbol: str) -> AIInsight:
        """Layer 5: TradingView external signals."""
        if not self.tv_receiver or not self.tv_config.enabled:
            return AIInsight(
                source="TradingView", score=0.0, signal="NEUTRAL",
                confidence=0.0, details="TV webhooks disabled",
            )

        try:
            consensus = self.tv_receiver.get_consensus(symbol)

            if consensus["active_signals"] == 0:
                return AIInsight(
                    source="TradingView", score=0.0, signal="NEUTRAL",
                    confidence=0.0, details=consensus["details"],
                )

            return AIInsight(
                source="TradingView",
                score=consensus["score"],
                signal=consensus["signal"],
                confidence=consensus["confidence"],
                details=consensus["details"],
            )
        except Exception as e:
            logger.debug(f"TradingView analysis failed: {e}")
            return AIInsight(
                source="TradingView", score=0.0, signal="NEUTRAL",
                confidence=0.0, details="TV signal error",
            )

    def _analyze_regime(self, adapted) -> AIInsight:
        """
        Layer 4: Market regime awareness — direction-aware scoring.

        TRENDING UP   → strong bullish bias (+0.30)
        TRENDING DOWN → bearish bias (-0.25)  [avoid buying into downtrends]
        RANGING       → neutral (mean-reversion still possible)
        VOLATILE      → slight negative bias (risk-off)
        QUIET         → neutral (wait for breakout)
        """
        direction = getattr(adapted, "trend_direction", "neutral")

        if adapted.regime == "TRENDING":
            if direction == "up":
                score = 0.30   # Strong directional uptrend — favour longs
                conf = 0.75
                label = "TRENDING ↑"
            else:
                score = -0.25  # Downtrend — do NOT buy
                conf = 0.75
                label = "TRENDING ↓"
        elif adapted.regime == "VOLATILE":
            score = -0.10  # Risk-off: volatility makes entries risky
            conf = 0.50
            label = "VOLATILE"
        elif adapted.regime == "RANGING":
            score = 0.0
            conf = 0.65
            label = "RANGING"
        else:  # QUIET
            score = 0.0
            conf = 0.60
            label = "QUIET"

        signal = "BUY" if score > 0.1 else ("SELL" if score < -0.1 else "HOLD")

        return AIInsight(
            source="Adaptive",
            score=round(score, 3),
            signal=signal,
            confidence=conf,
            details=f"Regime: {label} (size: {adapted.position_size_multiplier}x, SL: {adapted.stop_loss_pct}%)",
        )

    def _build_explanation(self, symbol, signal, score, insights, adapted, tech_data) -> str:
        """
        Build a clear, human-readable explanation for consultation mode.
        This is what the bot shows you before asking permission to trade.
        """
        lines = []
        emoji_map = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}

        lines.append(f"{'='*50}")
        lines.append(f"  {symbol} — AI ANALYSIS REPORT")
        lines.append(f"{'='*50}")
        lines.append(f"")
        lines.append(f"  Price: ${tech_data['price']:,.2f}")
        lines.append(f"  RSI:   {tech_data['rsi']}  |  EMA: {adapted.ema_fast}/{adapted.ema_slow}")
        lines.append(f"  Vol:   {tech_data['volume_ratio']}x  |  Regime: {adapted.regime}")
        lines.append(f"")
        lines.append(f"  ── AI Layers ──")

        for insight in insights:
            emoji = emoji_map.get(insight.signal, "⚪")
            lines.append(f"  {emoji} {insight.source:12s} → {insight.signal:5s} (score: {insight.score:+.2f}, conf: {insight.confidence:.0%})")
            lines.append(f"     {insight.details}")

        lines.append(f"")
        lines.append(f"  ── Decision ──")
        lines.append(f"  Combined AI Score: {score:+.3f}")
        lines.append(f"  Signal: {emoji_map.get(signal.value, '⚪')} {signal.value}")
        lines.append(f"")

        # Transaction cost breakdown
        cost_drag = self.costs.round_trip_fee_pct + self.costs.slippage_base_pct
        trade_amount = adapted.position_size_multiplier * 3
        est_fees = trade_amount * self.costs.effective_fee_rate * 2  # round trip
        est_slip = trade_amount * (self.costs.slippage_base_pct / 100)

        if signal == Signal.BUY:
            lines.append(f"  Would BUY ${trade_amount:.1f} USDT")
            lines.append(f"  Stop Loss: -{adapted.stop_loss_pct}% | Take Profit: +{adapted.take_profit_pct}%")
            lines.append(f"")
            lines.append(f"  ── Cost Estimate ──")
            lines.append(f"  Round-trip fees: ~${est_fees:.4f} ({self.costs.round_trip_fee_pct:.2f}%)")
            lines.append(f"  Est. slippage:   ~${est_slip:.4f} ({self.costs.slippage_base_pct}%)")
            lines.append(f"  Total cost drag: ~{cost_drag:.2f}%")
            lines.append(f"  Net TP target:   +{adapted.take_profit_pct - cost_drag:.2f}% (after costs)")
            lines.append(f"  Net SL risk:     -{adapted.stop_loss_pct + cost_drag:.2f}% (after costs)")
            # Risk/reward ratio with costs
            net_tp = adapted.take_profit_pct - cost_drag
            net_sl = adapted.stop_loss_pct + cost_drag
            rr = net_tp / net_sl if net_sl > 0 else 0
            lines.append(f"  Risk/Reward:     1:{rr:.2f} (net)")
        elif signal == Signal.SELL:
            lines.append(f"  Would CLOSE position")
            lines.append(f"  Exit fees: ~${est_fees/2:.4f} | Est. slippage: ~${est_slip/2:.4f}")

        lines.append(f"{'='*50}")

        return "\n".join(lines)

    def check_stop_loss_take_profit(
        self, entry_price: float, current_price: float, side: str,
        adapted_sl: float = None, adapted_tp: float = None,
        partial_tp_taken: bool = False,
    ) -> Optional[str]:
        """
        Check SL/TP with adaptive values, accounting for transaction costs.

        Partial TP logic (when config.partial_tp_enabled):
          - PARTIAL_TP fires at net_pnl >= tp/2  (e.g. 6.25% TP → fires at 3.125%)
          - After PARTIAL_TP taken, full TAKE_PROFIT fires at original tp target
          - Trailing stop still active on remaining position

        Returns: "STOP_LOSS" | "TAKE_PROFIT" | "PARTIAL_TP" | None
        """
        sl = adapted_sl or self.config.stop_loss_pct
        tp = adapted_tp or self.config.take_profit_pct

        # Total cost drag: round-trip fees + estimated slippage on exit
        cost_drag = self.costs.round_trip_fee_pct + self.costs.slippage_base_pct

        if side == "BUY":
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100

        # Net P&L after costs
        net_pnl_pct = pnl_pct - cost_drag

        # SL: trigger based on NET loss
        if net_pnl_pct <= -sl:
            logger.info(f"SL: gross {pnl_pct:+.2f}% → net {net_pnl_pct:+.2f}% (limit: -{sl}%)")
            return "STOP_LOSS"

        # Partial TP: fire at 50% of TP target (only once, before full TP)
        if self.config.partial_tp_enabled and not partial_tp_taken:
            partial_target = tp / 2
            if net_pnl_pct >= partial_target:
                logger.info(f"PARTIAL_TP: net {net_pnl_pct:+.2f}% ≥ {partial_target:.2f}% (50% of TP {tp}%)")
                return "PARTIAL_TP"

        # Full TP: trigger based on NET profit
        if net_pnl_pct >= tp:
            logger.info(f"TP: gross {pnl_pct:+.2f}% → net {net_pnl_pct:+.2f}% (target: +{tp}%)")
            return "TAKE_PROFIT"

        return None
