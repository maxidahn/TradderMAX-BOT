"""
Momentum Hunter Agent
======================
Filosofía: "El precio en movimiento tiende a seguir en movimiento".

Setup ideal:
  - ADX ≥ adx_min (hay tendencia)
  - EMA9 > EMA21 (alcista) o EMA9 < EMA21 (bajista) → dirección
  - Volume ratio ≥ volume_min_ratio (participación real)
  - Funding rate moderado (no extremo en contra)
  - RSI no en zona contraria extrema

Acciones:
  - LONG cuando los 4 alinean al alza
  - SHORT cuando los 4 alinean a la baja
  - FLAT si falta convicción
"""

import logging
import pandas as pd

from agents.base_agent import BaseAgent, AgentDecision, Action, compute_common_features

logger = logging.getLogger("celerity.agent.momentum")


class MomentumAgent(BaseAgent):

    def __init__(self, params, perpetuals_data=None, replay_buffer=None):
        super().__init__("MomentumHunter", params, perpetuals_data, replay_buffer)

    def decide(self, symbol: str, candles: pd.DataFrame) -> AgentDecision:
        feats = compute_common_features(candles)
        if not feats:
            return self._flat(symbol, feats, "insufficient data")

        # ── Funding rate context (penaliza ir con el rebaño) ─────────────────
        funding_rate = 0.0
        ls_ratio = 1.0
        if self.perp:
            m = self.perp.get_metrics(symbol)
            if m:
                funding_rate = m.funding_rate
                ls_ratio = m.long_short_ratio
                feats["funding_rate"] = funding_rate
                feats["ls_ratio"]     = ls_ratio

        # ── Conditions for LONG ──────────────────────────────────────────────
        bullish_aligned = (
            feats["adx"] >= self.params.adx_min and
            feats["ema_spread_pct"] > 0.1 and
            feats["trend_dir"] == "up" and
            feats["volume_ratio"] >= self.params.volume_min_ratio and
            feats["rsi"] > 40 and feats["rsi"] < 70 and
            feats["rsi_momentum"] >= 0
        )
        # No entrar long si funding está muy positivo (longs sobrepagando)
        if bullish_aligned and funding_rate > 0.04:
            bullish_aligned = False
            feats["blocked_long_by_funding"] = funding_rate

        # ── Conditions for SHORT ─────────────────────────────────────────────
        bearish_aligned = (
            feats["adx"] >= self.params.adx_min and
            feats["ema_spread_pct"] < -0.1 and
            feats["trend_dir"] == "down" and
            feats["volume_ratio"] >= self.params.volume_min_ratio and
            feats["rsi"] < 60 and feats["rsi"] > 30 and
            feats["rsi_momentum"] <= 0
        )
        # No entrar short si funding está muy negativo
        if bearish_aligned and funding_rate < -0.04:
            bearish_aligned = False
            feats["blocked_short_by_funding"] = funding_rate

        # ── Score → confidence ───────────────────────────────────────────────
        # Confidence empieza en 0.50 base y crece con la fuerza de los indicadores
        if bullish_aligned or bearish_aligned:
            adx_norm = min(feats["adx"] / 40.0, 1.0)              # 0..1 (40=fuerte)
            spread_norm = min(abs(feats["ema_spread_pct"]) / 1.5, 1.0)  # 0..1 (1.5%=máx)
            vol_norm = min(feats["volume_ratio"] / 2.5, 1.0)      # 0..1 (2.5x=máx)
            confidence = 0.40 + 0.20 * adx_norm + 0.20 * spread_norm + 0.20 * vol_norm

            # Cross-learning legacy (lectura del replay buffer)
            confidence *= self.confidence_multiplier_from_replay()
            # Aprendizaje acelerado: OnlineML + Contagion (#2 + #5)
            confidence, boost_details = self.apply_learning_boost(confidence, feats)

            if bullish_aligned:
                action = Action.LONG
                reasoning = (
                    f"ADX {feats['adx']:.0f} ≥ {self.params.adx_min} + "
                    f"EMA9>EMA21 ({feats['ema_spread_pct']:+.2f}%) + "
                    f"vol {feats['volume_ratio']:.1f}x + RSI {feats['rsi']:.0f}↑"
                )
            else:
                action = Action.SHORT
                reasoning = (
                    f"ADX {feats['adx']:.0f} ≥ {self.params.adx_min} + "
                    f"EMA9<EMA21 ({feats['ema_spread_pct']:+.2f}%) + "
                    f"vol {feats['volume_ratio']:.1f}x + RSI {feats['rsi']:.0f}↓"
                )

            if boost_details:
                reasoning += f" [{boost_details}]"

            return AgentDecision(
                agent_name=self.name,
                symbol=symbol,
                action=action,
                confidence=confidence,
                features=feats,
                reasoning=reasoning,
                sl_pct=self.params.sl_pct,
                tp_pct=self.params.tp_pct,
            )

        return self._flat(symbol, feats,
                          f"no alignment (ADX {feats.get('adx', 0):.0f}, "
                          f"spread {feats.get('ema_spread_pct', 0):+.2f}%, "
                          f"vol {feats.get('volume_ratio', 0):.1f}x)")

    def _flat(self, symbol, feats, reason) -> AgentDecision:
        return AgentDecision(
            agent_name=self.name,
            symbol=symbol,
            action=Action.FLAT,
            confidence=0.0,
            features=feats,
            reasoning=reason,
        )
