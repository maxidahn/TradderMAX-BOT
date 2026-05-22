"""
Adjudicator — Claude resuelve conflictos entre agentes
=========================================================
Solo se invoca cuando los dos agentes dan señales OPUESTAS en el mismo símbolo
(uno LONG, el otro SHORT). Claude evalúa el contexto macro y dice cuál tiene
razón — o si lo correcto es no entrar.

Si los dos agentes coinciden (LONG+LONG, SHORT+SHORT, FLAT+algo) → no se llama.
Esto mantiene el costo de Claude bajo control.

Decisión de Claude:
  - winner: "MomentumHunter" / "ReversalSniper" / "BOTH_WAIT"
  - confidence: 0.0 – 1.0
  - reasoning: texto breve
"""

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger("celerity.adjudicator")

# Rate-limit: máximo 1 llamada cada 60s al adjudicator (los conflictos rara vez son urgentes)
MIN_CALL_INTERVAL = 60


class Adjudicator:

    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.client  = None
        self.enabled = False
        self._last_call: float = 0.0
        self._call_count = 0
        self._init_client()

    def _init_client(self):
        if not self.api_key:
            logger.warning("Adjudicator: no ANTHROPIC_API_KEY — falls back to confidence comparison")
            return
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            self.enabled = True
            logger.info("Adjudicator: Claude initialized ✓")
        except Exception as e:
            logger.error(f"Adjudicator: failed to init: {e}")

    def resolve_conflict(self, decision_a, decision_b, perpetuals_metrics: Optional[dict] = None) -> dict:
        """
        Resuelve un conflicto entre dos decisiones contradictorias.

        Si Claude está deshabilitado o falla, fallback: el de mayor confidence gana.
        """
        # Sanity check
        if decision_a.action == decision_b.action:
            return {
                "winner":     decision_a.agent_name if decision_a.confidence >= decision_b.confidence else decision_b.agent_name,
                "confidence": max(decision_a.confidence, decision_b.confidence),
                "reasoning":  "Both agents agree — no conflict",
                "source":     "no_conflict",
            }

        # Fallback if Claude not available
        if not self.enabled or not self.client:
            return self._fallback_resolution(decision_a, decision_b)

        # Rate limit
        now = time.time()
        if now - self._last_call < MIN_CALL_INTERVAL:
            return self._fallback_resolution(decision_a, decision_b)

        try:
            prompt = self._build_prompt(decision_a, decision_b, perpetuals_metrics)
            msg = self.client.messages.create(
                model="claude-haiku-4-5-20251001",   # Haiku para conflictos (barato y rápido)
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            result = self._parse_response(text, decision_a, decision_b)
            self._last_call = now
            self._call_count += 1
            logger.info(
                f"[Adjudicator] {decision_a.symbol}: {decision_a.agent_name}({decision_a.action.value}) "
                f"vs {decision_b.agent_name}({decision_b.action.value}) → "
                f"winner={result['winner']} ({result['reasoning'][:60]})"
            )
            return result
        except Exception as e:
            logger.error(f"Adjudicator API call failed: {e}")
            return self._fallback_resolution(decision_a, decision_b)

    def _build_prompt(self, a, b, perp) -> str:
        funding   = perp.get("funding_rate", 0)    if perp else 0
        ls_ratio  = perp.get("long_short_ratio", 1) if perp else 1
        oi_usdt   = perp.get("open_interest_usdt", 0) if perp else 0

        return f"""You are a crypto perpetuals trading adjudicator. Two algorithmic agents disagree on the same symbol and timeframe. Decide which is correct or if both should wait.

SYMBOL: {a.symbol}

AGENT A — {a.agent_name}
  Action: {a.action.value}
  Confidence: {a.confidence:.2f}
  Reasoning: {a.reasoning}
  Features snapshot: {json.dumps({k: round(v, 4) if isinstance(v, (int, float)) else v for k, v in a.features.items() if k in ('price','rsi','adx','ema_spread_pct','bb_position','volume_ratio')})}

AGENT B — {b.agent_name}
  Action: {b.action.value}
  Confidence: {b.confidence:.2f}
  Reasoning: {b.reasoning}

PERPETUAL CONTEXT:
  Funding rate: {funding:+.4f}% (positive = longs paying shorts)
  Long/Short ratio: {ls_ratio:.2f}
  Open interest USDT: ${oi_usdt:,.0f}

Respond ONLY with this JSON (no markdown, no extra text):
{{
  "winner": "{a.agent_name}" | "{b.agent_name}" | "BOTH_WAIT",
  "confidence": <float 0-1>,
  "reasoning": "<one sentence, max 120 chars>"
}}

Pick BOTH_WAIT if the conflicting signals indicate genuine market uncertainty (e.g. price stuck in a tight range with no clear edge)."""

    def _parse_response(self, text: str, a, b) -> dict:
        try:
            # Strip markdown
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            # JSON doesn't allow leading + on numbers
            text = re.sub(r':\s*\+(\d)', r': \1', text)
            data = json.loads(text.strip())
            winner = data.get("winner", "BOTH_WAIT")
            if winner not in (a.agent_name, b.agent_name, "BOTH_WAIT"):
                winner = "BOTH_WAIT"
            return {
                "winner":     winner,
                "confidence": max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
                "reasoning":  str(data.get("reasoning", ""))[:200],
                "source":     "claude_adjudicator",
            }
        except Exception as e:
            logger.warning(f"Adjudicator parse failed ({e}): {text[:120]}")
            return self._fallback_resolution(a, b)

    def _fallback_resolution(self, a, b) -> dict:
        """Sin Claude: si la diferencia de confidence es chica → BOTH_WAIT."""
        diff = abs(a.confidence - b.confidence)
        if diff < 0.10:
            return {
                "winner":     "BOTH_WAIT",
                "confidence": 0.4,
                "reasoning":  f"Confidences too close ({a.confidence:.2f} vs {b.confidence:.2f}) — abstain",
                "source":     "fallback",
            }
        if a.confidence > b.confidence:
            return {
                "winner":     a.agent_name,
                "confidence": a.confidence,
                "reasoning":  f"Higher confidence ({a.confidence:.2f} > {b.confidence:.2f})",
                "source":     "fallback",
            }
        return {
            "winner":     b.agent_name,
            "confidence": b.confidence,
            "reasoning":  f"Higher confidence ({b.confidence:.2f} > {a.confidence:.2f})",
            "source":     "fallback",
        }

    def get_stats(self) -> dict:
        return {
            "enabled":    self.enabled,
            "call_count": self._call_count,
            "last_call_ago_sec": int(time.time() - self._last_call) if self._last_call else None,
        }
