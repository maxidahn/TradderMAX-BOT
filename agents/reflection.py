"""
Reflection — Post-mortem semántico con Claude Haiku tras cada cierre
======================================================================
Después de CADA trade cerrado (gane o pierda), llamamos a Claude con:
  - Features del momento de entrada
  - Decisión del agente y su razonamiento
  - Outcome real (pnl, hold time, exit reason)
  - Parámetros vivos del agente

Claude responde con UNA recomendación: "subí adx_min 2 puntos" / "no cambies".
Acumulamos las recomendaciones por (agente, parámetro). Si 3 cierres
consecutivos sugieren la misma dirección en el mismo parámetro → aplicar.

Esto es aprendizaje semántico: Claude entiende patrones que el GBM no
detecta porque tiene contexto del razonamiento original del agente.

Costo: ~$0.001 por reflexión × 100 trades/día = $0.10/día.
"""

import json
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("celerity.reflection")

DATA_DIR = os.getenv("DATA_DIR", "data")
LOG_FILE = os.path.join(DATA_DIR, "reflections_log.json")

# Solo permitimos ajuste sobre estos parámetros (los demás son estructurales)
TUNABLE_PARAMS = {
    "min_confidence":        (0.30, 0.85, 0.02),    # (min, max, step)
    "sl_pct":                (0.5, 4.0, 0.1),
    "tp_pct":                (1.0, 8.0, 0.2),
    "max_hold_minutes":      (30, 480, 10),
    "trailing_after_pct":    (0.5, 3.0, 0.1),
    "trailing_distance_pct": (0.3, 2.0, 0.1),
    "adx_min":               (15.0, 40.0, 1.0),
    "volume_min_ratio":      (0.5, 2.5, 0.1),
    "rsi_extreme_low":       (15.0, 35.0, 1.0),
    "rsi_extreme_high":      (65.0, 85.0, 1.0),
    "funding_extreme":       (0.01, 0.10, 0.005),
}


class ReflectionRecord:
    """Una recomendación individual de Claude."""

    __slots__ = ("agent", "param", "direction", "delta", "reasoning", "timestamp")

    def __init__(self, agent, param, direction, delta, reasoning):
        self.agent     = agent
        self.param     = param
        self.direction = direction      # "up" / "down" / "none"
        self.delta     = delta
        self.reasoning = reasoning
        self.timestamp = datetime.now(timezone.utc).isoformat()


class Reflector:

    def __init__(self, model: str = "claude-haiku-4-5-20251001",
                  apply_threshold: int = 3,
                  max_delta_pct: float = 0.10):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.client  = None
        self.enabled = False
        self.model   = model
        self.apply_threshold = apply_threshold
        self.max_delta_pct   = max_delta_pct

        # Cola por (agent, param) — últimas 5 recomendaciones
        self._suggestion_queue: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=5))
        # Historial completo (cap 200) para dashboard
        self._history: List[ReflectionRecord] = []
        # Stats
        self._call_count = 0
        self._applied_count = 0
        self._init_client()

    def _init_client(self):
        if not self.api_key:
            logger.warning("Reflector: no ANTHROPIC_API_KEY — reflection disabled")
            return
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=self.api_key)
            self.enabled = True
            logger.info(f"Reflector: initialized ({self.model}) ✓")
        except Exception as e:
            logger.error(f"Reflector init failed: {e}")

    def reflect(self, trade_record, decision_entry: Optional[dict],
                 agent_params: dict) -> Optional[dict]:
        """
        Llamado tras cada cierre. Devuelve una recomendación (o None si Claude no responde).

        trade_record: FuturesTradeRecord
        decision_entry: entry del replay buffer (decisión original)
        agent_params: parámetros vivos del agente (dict)
        """
        if not self.enabled or not self.client:
            return None

        try:
            prompt = self._build_prompt(trade_record, decision_entry, agent_params)
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            rec  = self._parse_response(text, trade_record.agent)
            self._call_count += 1

            if not rec:
                return None

            # Encolar
            key = (rec.agent, rec.param)
            self._suggestion_queue[key].append(rec)
            self._history.append(rec)
            if len(self._history) > 200:
                self._history = self._history[-200:]

            logger.info(
                f"[Reflector] {rec.agent}/{rec.param}: {rec.direction} "
                f"({rec.delta:+.3f}) — {rec.reasoning[:80]}"
            )
            self._save_log()
            return {
                "agent":     rec.agent,
                "param":     rec.param,
                "direction": rec.direction,
                "delta":     rec.delta,
                "reasoning": rec.reasoning,
            }
        except Exception as e:
            logger.error(f"Reflector.reflect failed: {e}")
            return None

    def check_and_apply(self, agent) -> Optional[dict]:
        """
        Revisa la cola de un agente y aplica si hay suficientes recomendaciones
        consecutivas en la misma dirección sobre el mismo parámetro.
        Devuelve dict con el cambio aplicado, o None.
        """
        for (a_name, param), queue in self._suggestion_queue.items():
            if a_name != agent.name:
                continue
            if param not in TUNABLE_PARAMS:
                continue
            recent = list(queue)
            if len(recent) < self.apply_threshold:
                continue

            # Las últimas N recomendaciones deben ir en la misma dirección
            last_n = recent[-self.apply_threshold:]
            directions = [r.direction for r in last_n]
            if len(set(directions)) != 1 or directions[0] == "none":
                continue

            direction = directions[0]
            # Magnitud: promedio de los deltas sugeridos, pero clampeado al max_delta_pct
            avg_delta = sum(r.delta for r in last_n) / len(last_n)
            current = getattr(agent.params, param, None)
            if current is None:
                continue

            max_change = abs(current) * self.max_delta_pct if current != 0 else 0.01
            applied_delta = max(-max_change, min(max_change, avg_delta))
            new_value = current + (applied_delta if direction == "up" else -abs(applied_delta))

            # Clamp al rango
            lo, hi, _step = TUNABLE_PARAMS[param]
            new_value = max(lo, min(hi, new_value))
            if isinstance(current, int):
                new_value = int(round(new_value))

            if abs(new_value - current) < 1e-6:
                continue   # Sin cambio efectivo

            # Aplicar
            setattr(agent.params, param, new_value)
            self._applied_count += 1
            queue.clear()    # Reset para que no se reaplique inmediatamente

            change = {
                "agent":     agent.name,
                "param":     param,
                "from":      current,
                "to":        new_value,
                "direction": direction,
                "rationale": f"{self.apply_threshold} cierres consecutivos sugirieron {direction}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(f"[Reflector] APPLIED {agent.name}.{param}: {current} → {new_value} ({direction})")
            self._save_log(extra_event=change)
            return change
        return None

    def _build_prompt(self, trade_record, decision_entry, agent_params) -> str:
        features_summary = ""
        if decision_entry and decision_entry.get("features"):
            feats = decision_entry["features"]
            # Filtrar solo numéricas
            f_clean = {k: round(v, 4) for k, v in feats.items() if isinstance(v, (int, float))}
            features_summary = json.dumps(f_clean, indent=2)[:600]

        reasoning_original = (decision_entry or {}).get("reasoning", "")

        return f"""You are a trading systems analyst. A perpetual futures trade just closed. Analyze if any parameter of the agent should be adjusted.

TRADE OUTCOME:
  Symbol: {trade_record.symbol}
  Side: {trade_record.side}
  Agent: {trade_record.agent}
  Entry: ${trade_record.entry_price:.4f} → Exit: ${trade_record.exit_price:.4f}
  PnL: ${trade_record.pnl_usdt:+.4f} ({trade_record.pnl_pct:+.2f}%)
  Hold time: {trade_record.hold_minutes:.0f} min
  Exit reason: {trade_record.reason}
  Leverage: {trade_record.leverage}x

ORIGINAL DECISION REASONING:
  {reasoning_original[:300]}

ENTRY FEATURES SNAPSHOT:
{features_summary}

AGENT CURRENT PARAMETERS:
{json.dumps(agent_params, indent=2)[:500]}

TUNABLE PARAMETERS (the only ones you can suggest changing):
{list(TUNABLE_PARAMS.keys())}

Respond ONLY in this exact JSON format (no markdown, no extra text):
{{
  "param": "<one of the tunable parameters, or 'none' if no change needed>",
  "direction": "up" | "down" | "none",
  "delta": <float — magnitude of suggested change in absolute units of the parameter>,
  "reasoning": "<one sentence, max 100 chars, explaining WHY>"
}}

Rules:
- If the trade was profitable and the setup was sound → likely "none" (don't fix what works)
- If SL hit too fast and the original setup looked correct → suggest "sl_pct" "up"
- If TP missed by a small amount → suggest "tp_pct" "down" slightly
- If trade closed by TIMEOUT — neither winner nor SL — suggest "max_hold_minutes" "down"
- If too many low-confidence trades losing → "min_confidence" "up"
- Be conservative: only suggest a change if there's a clear pattern from this trade."""

    def _parse_response(self, text: str, agent_name: str) -> Optional[ReflectionRecord]:
        try:
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = re.sub(r':\s*\+(\d)', r': \1', text)
            data = json.loads(text.strip())

            param = data.get("param", "none")
            direction = data.get("direction", "none")

            if param == "none" or direction == "none":
                return ReflectionRecord(agent_name, "none", "none", 0.0,
                                         str(data.get("reasoning", ""))[:200])

            if param not in TUNABLE_PARAMS:
                logger.debug(f"[Reflector] suggested non-tunable param: {param}")
                return None

            return ReflectionRecord(
                agent_name, param, direction,
                float(data.get("delta", 0.0)),
                str(data.get("reasoning", ""))[:200],
            )
        except Exception as e:
            logger.debug(f"[Reflector] parse failed ({e}): {text[:120]}")
            return None

    def _save_log(self, extra_event: Optional[dict] = None):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            data = {
                "history": [
                    {
                        "agent":     r.agent,
                        "param":     r.param,
                        "direction": r.direction,
                        "delta":     r.delta,
                        "reasoning": r.reasoning,
                        "timestamp": r.timestamp,
                    }
                    for r in self._history[-100:]
                ],
                "stats": {
                    "calls":   self._call_count,
                    "applied": self._applied_count,
                },
            }
            if extra_event:
                data.setdefault("applied_events", []).append(extra_event)
            with open(LOG_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"[Reflector] log save failed: {e}")

    def get_status(self) -> dict:
        return {
            "enabled":   self.enabled,
            "model":     self.model,
            "calls":     self._call_count,
            "applied":   self._applied_count,
            "recent":    [
                {
                    "agent":     r.agent,
                    "param":     r.param,
                    "direction": r.direction,
                    "delta":     r.delta,
                    "reasoning": r.reasoning,
                    "timestamp": r.timestamp,
                }
                for r in self._history[-20:]
            ],
        }
