"""
Reversal Sniper Agent
======================
Filosofía: "Los extremos no duran. Vende euforia, compra pánico".

Setup ideal:
  - RSI ≤ rsi_extreme_low (sobreventa) → LONG (mean reversion)
  - RSI ≥ rsi_extreme_high (sobrecompra) → SHORT
  - Bollinger touch (precio ≤ bb_lower o ≥ bb_upper) — confirma extremo
  - Funding extremo en contra del retail (longs muy positivos → SHORT, etc.)
  - L/S ratio extremo del retail (>3 o <0.4) — contra el sesgo del rebaño

Targets cortos (sniper):
  - TP rápido (2.0–2.5%)
  - SL apretado (1.0–1.5%)
  - Hold máximo corto (60–120 min)
"""

import logging
import pandas as pd

from agents.base_agent import BaseAgent, AgentDecision, Action, compute_common_features

logger = logging.getLogger("celerity.agent.reversion")


class ReversionAgent(BaseAgent):

    def __init__(self, params, perpetuals_data=None, replay_buffer=None):
        super().__init__("ReversalSniper", params, perpetuals_data, replay_buffer)

    def decide(self, symbol: str, candles: pd.DataFrame) -> AgentDecision:
        feats = compute_common_features(candles)
        if not feats:
            return self._flat(symbol, feats, "insufficient data")

        # ── Funding & positioning context (alfa real del sniper) ─────────────
        funding_rate = 0.0
        ls_ratio = 1.0
        funding_signal = None
        if self.perp:
            m = self.perp.get_metrics(symbol)
            if m:
                funding_rate = m.funding_rate
                ls_ratio = m.long_short_ratio
                feats["funding_rate"] = funding_rate
                feats["ls_ratio"] = ls_ratio
                feats["top_trader_ls_ratio"] = m.top_trader_ls_ratio

                # funding_extreme triggers contra-trade
                if funding_rate >= self.params.funding_extreme:
                    funding_signal = Action.SHORT   # longs sobrepagando → vender
                elif funding_rate <= -self.params.funding_extreme:
                    funding_signal = Action.LONG    # shorts sobrepagando → comprar

        # ── LONG conditions (sobreventa) ─────────────────────────────────────
        rsi_oversold = feats["rsi"] <= self.params.rsi_extreme_low
        bb_touch_low = feats["bb_position"] <= -0.9
        # Crowded shorts (rebaño bajista) → contra-trade LONG
        crowded_short = ls_ratio <= 0.4

        long_score = 0.0
        if rsi_oversold:        long_score += 0.35
        if bb_touch_low:        long_score += 0.30
        if crowded_short:       long_score += 0.15
        if funding_signal == Action.LONG: long_score += 0.30
        # 2026-05-18 EXPLORATION BOOST v2: penalización eliminada.
        # En mercados bear, el Sniper DEBE poder operar contra-tendencia (es su rol).
        # Originalmente: if adx>30 and trend_dir==down: long_score -= 0.20

        # ── SHORT conditions (sobrecompra) ───────────────────────────────────
        rsi_overbought = feats["rsi"] >= self.params.rsi_extreme_high
        bb_touch_high  = feats["bb_position"] >= 0.9
        crowded_long   = ls_ratio >= 3.0

        short_score = 0.0
        if rsi_overbought:      short_score += 0.35
        if bb_touch_high:       short_score += 0.30
        if crowded_long:        short_score += 0.15
        if funding_signal == Action.SHORT: short_score += 0.30
        # 2026-05-18 EXPLORATION BOOST v2: penalización eliminada (simétrica al LONG).
        # Originalmente: if adx>30 and trend_dir==up: short_score -= 0.20

        # ── Decide ───────────────────────────────────────────────────────────
        # Cross-learning legacy multiplier
        replay_mult = self.confidence_multiplier_from_replay()

        # ⚡ EXPLORATION BOOST 2026-05-18 v2 — gate bajado 0.40 → 0.30 (Sniper sigue dormido)
        if long_score >= 0.30 and long_score > short_score:
            base_conf = min(long_score * replay_mult, 1.0)
            # Aprendizaje acelerado: OnlineML + Contagion (#2 + #5)
            confidence, boost_details = self.apply_learning_boost(base_conf, feats)
            reasoning_parts = []
            if rsi_oversold:         reasoning_parts.append(f"RSI {feats['rsi']:.0f} oversold")
            if bb_touch_low:         reasoning_parts.append(f"BB touch low ({feats['bb_position']:+.2f})")
            if crowded_short:        reasoning_parts.append(f"crowded SHORT (L/S {ls_ratio:.2f})")
            if funding_signal == Action.LONG: reasoning_parts.append(f"funding {funding_rate:+.3f}%")
            reasoning = " + ".join(reasoning_parts) or "long score reached"
            if boost_details:
                reasoning += f" [{boost_details}]"
            return AgentDecision(
                agent_name=self.name,
                symbol=symbol,
                action=Action.LONG,
                confidence=confidence,
                features=feats,
                reasoning=reasoning,
                sl_pct=self.params.sl_pct,
                tp_pct=self.params.tp_pct,
            )

        if short_score >= 0.30 and short_score > long_score:
            base_conf = min(short_score * replay_mult, 1.0)
            confidence, boost_details = self.apply_learning_boost(base_conf, feats)
            reasoning_parts = []
            if rsi_overbought:       reasoning_parts.append(f"RSI {feats['rsi']:.0f} overbought")
            if bb_touch_high:        reasoning_parts.append(f"BB touch high ({feats['bb_position']:+.2f})")
            if crowded_long:         reasoning_parts.append(f"crowded LONG (L/S {ls_ratio:.2f})")
            if funding_signal == Action.SHORT: reasoning_parts.append(f"funding {funding_rate:+.3f}%")
            reasoning = " + ".join(reasoning_parts) or "short score reached"
            if boost_details:
                reasoning += f" [{boost_details}]"
            return AgentDecision(
                agent_name=self.name,
                symbol=symbol,
                action=Action.SHORT,
                confidence=confidence,
                features=feats,
                reasoning=reasoning,
                sl_pct=self.params.sl_pct,
                tp_pct=self.params.tp_pct,
            )

        return self._flat(symbol, feats,
                          f"no reversal (RSI {feats.get('rsi', 50):.0f}, "
                          f"BB {feats.get('bb_position', 0):+.2f}, "
                          f"funding {funding_rate:+.3f}%, L/S {ls_ratio:.2f})")

    def _flat(self, symbol, feats, reason) -> AgentDecision:
        return AgentDecision(
            agent_name=self.name,
            symbol=symbol,
            action=Action.FLAT,
            confidence=0.0,
            features=feats,
            reasoning=reason,
        )
