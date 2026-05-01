"""
Celerity Trader Bot - Claude AI Agent (Layer 5)
================================================
Uses Claude Sonnet as a contextual reasoning layer.
Only activates when other layers detect a potential signal (score > 0.15),
acting as a final filter before proposing a trade.

Claude receives:
  - Current market data (price, RSI, EMA, volume, regime)
  - Scores from the 4 existing layers
  - Recent trade history and P&L context
  - Open positions

Claude returns:
  - Recommendation: BUY / SELL / HOLD
  - Confidence: 0.0 - 1.0
  - Score adjustment: -0.3 to +0.3
  - Reasoning: plain text explanation
"""

import json
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger("celerity.claude")

# Only activate Claude when base score exceeds this threshold
ACTIVATION_THRESHOLD = 0.15
# Claude's weight in the final score
CLAUDE_WEIGHT = 0.20
# Minimum seconds between Claude calls per symbol (avoid hammering the API)
MIN_CALL_INTERVAL = 120  # 2 minutes


class ClaudeAgent:
    """Wraps Claude Sonnet as a trading analysis agent."""

    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.client = None
        self.enabled = False
        self._last_call: Dict[str, float] = {}
        self._last_result: Dict[str, dict] = {}
        self._call_count = 0
        self._init_client()

    def _init_client(self):
        if not self.api_key:
            logger.warning("Claude Agent: ANTHROPIC_API_KEY not set — layer disabled")
            return
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            self.enabled = True
            logger.info("Claude Agent: Sonnet layer initialized ✓")
        except ImportError:
            logger.warning("Claude Agent: anthropic package not installed — run: pip install anthropic")
        except Exception as e:
            logger.error(f"Claude Agent: failed to initialize: {e}")

    def analyze(
        self,
        symbol: str,
        price: float,
        rsi: float,
        ema_fast: float,
        ema_slow: float,
        volume_ratio: float,
        regime: str,
        base_score: float,
        layer_scores: dict,
        trade_history_summary: Optional[dict] = None,
        open_positions: Optional[list] = None,
    ) -> dict:
        """
        Ask Claude to evaluate a potential trade.
        Returns dict with: score_adjustment, confidence, recommendation, reasoning.
        """
        # Return cached result if called too recently for this symbol
        now = time.time()
        last = self._last_call.get(symbol, 0)
        if now - last < MIN_CALL_INTERVAL and symbol in self._last_result:
            cached = self._last_result[symbol]
            logger.debug(f"Claude Agent [{symbol}]: using cached result ({int(now-last)}s old)")
            return cached

        if not self.enabled or not self.client:
            return self._neutral()

        # Only activate above threshold
        if abs(base_score) < ACTIVATION_THRESHOLD:
            return self._neutral()

        try:
            prompt = self._build_prompt(
                symbol, price, rsi, ema_fast, ema_slow,
                volume_ratio, regime, base_score, layer_scores,
                trade_history_summary, open_positions,
            )

            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = message.content[0].text.strip()
            result = self._parse_response(response_text)
            self._last_call[symbol] = now
            self._last_result[symbol] = result
            self._call_count += 1

            logger.info(
                f"Claude Agent [{symbol}]: {result['recommendation']} "
                f"(adj: {result['score_adjustment']:+.2f}, conf: {result['confidence']:.0%}) "
                f"— {result['reasoning'][:80]}..."
            )
            return result

        except Exception as e:
            logger.error(f"Claude Agent [{symbol}]: API error — {e}")
            return self._neutral()

    def _build_prompt(
        self, symbol, price, rsi, ema_fast, ema_slow,
        volume_ratio, regime, base_score, layer_scores,
        trade_history_summary, open_positions,
    ) -> str:
        history_context = ""
        if trade_history_summary and trade_history_summary.get("total_trades", 0) > 0:
            history_context = f"""
Recent performance:
- Total trades: {trade_history_summary['total_trades']}
- Win rate: {trade_history_summary['win_rate']}%
- Total P&L: ${trade_history_summary['total_pnl']}
"""

        positions_context = ""
        if open_positions:
            positions_context = f"\nCurrently holding: {', '.join(open_positions)}"

        layers_text = "\n".join(
            f"  - {k}: {v:+.3f}" for k, v in layer_scores.items()
        )

        direction = "BUY" if base_score > 0 else "SELL"

        return f"""You are a crypto trading risk analyst for a live trading bot. Evaluate whether to proceed with this potential {direction} signal.

MARKET DATA ({symbol}):
- Price: ${price:,.2f}
- RSI: {rsi:.1f} (>70 overbought, <30 oversold)
- EMA Fast/Slow: {ema_fast:.2f} / {ema_slow:.2f} ({'bullish crossover' if ema_fast > ema_slow else 'bearish crossover'})
- Volume ratio: {volume_ratio:.2f}x average
- Market regime: {regime}

AI LAYER SCORES (combined: {base_score:+.3f}):
{layers_text}
{history_context}{positions_context}

Respond in this exact JSON format (nothing else):
{{
  "recommendation": "BUY" | "HOLD" | "SELL",
  "score_adjustment": <float between -0.30 and +0.30>,
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one sentence, max 100 chars>"
}}

Be conservative. Only boost score if conditions are genuinely favorable. Penalize if RSI is extreme, volume is weak, or regime is unfavorable for the signal direction."""

    def _parse_response(self, text: str) -> dict:
        """Parse Claude's JSON response."""
        try:
            # Extract JSON block if wrapped in markdown
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())
            return {
                "recommendation":  data.get("recommendation", "HOLD"),
                "score_adjustment": max(-0.30, min(0.30, float(data.get("score_adjustment", 0)))),
                "confidence":       max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
                "reasoning":        str(data.get("reasoning", ""))[:200],
                "source":           "Claude Sonnet",
            }
        except Exception as e:
            logger.warning(f"Claude Agent: could not parse response ({e}): {text[:100]}")
            return self._neutral()

    def _neutral(self) -> dict:
        return {
            "recommendation":   "HOLD",
            "score_adjustment": 0.0,
            "confidence":       0.5,
            "reasoning":        "No Claude analysis",
            "source":           "Claude Sonnet",
        }

    def get_stats(self) -> dict:
        return {
            "enabled":     self.enabled,
            "call_count":  self._call_count,
            "model":       "claude-sonnet-4-6",
        }
