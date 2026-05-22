"""
Replay Buffer — Memoria compartida entre agentes
==================================================
Cada decisión + outcome se persiste en un archivo JSONL (append-only).
Esta es la "experiencia común" desde la cual los dos agentes aprenden.

Schema de cada entrada:
{
  "id": "...",                     # uuid
  "timestamp": "iso",
  "agent": "MomentumHunter",       # quién decidió
  "symbol": "SOLUSDT",
  "action": "LONG"/"SHORT"/"FLAT",
  "confidence": 0.75,
  "features": {...},               # snapshot de inputs
  "reasoning": "...",
  "executed": true,                # si se llegó a abrir posición
  "params_snapshot": {...},        # parámetros del agente en ese momento
  "outcome": {                     # añadido cuando la posición cierra
    "pnl_usdt": 1.23,
    "pnl_pct": 1.4,
    "exit_reason": "TP",
    "hold_minutes": 45,
    "exit_price": 165.43,
  }
}
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("celerity.replay")

DATA_DIR = os.getenv("DATA_DIR", "data")
BUFFER_FILE = os.path.join(DATA_DIR, "replay_buffer.jsonl")

# Hard cap — buffer crece sin parar si no acotamos. 5000 entries ≈ 2MB.
MAX_ENTRIES = 5000


class ReplayBuffer:
    """Append-only JSONL buffer compartido entre agentes."""

    def __init__(self, path: str = BUFFER_FILE):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._cache: List[dict] = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            self._cache = []
            return
        try:
            with open(self.path, "r") as f:
                lines = f.readlines()
            entries = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            # Trim if too large (keep last MAX_ENTRIES)
            if len(entries) > MAX_ENTRIES:
                entries = entries[-MAX_ENTRIES:]
                self._rewrite(entries)
            self._cache = entries
            logger.info(f"ReplayBuffer: loaded {len(entries)} entries from {self.path}")
        except Exception as e:
            logger.error(f"ReplayBuffer load failed: {e}")
            self._cache = []

    def _rewrite(self, entries: List[dict]):
        """Trim the file when it grows past MAX_ENTRIES."""
        try:
            with open(self.path, "w") as f:
                for e in entries:
                    f.write(json.dumps(e) + "\n")
        except Exception as e:
            logger.warning(f"ReplayBuffer rewrite failed: {e}")

    def record_decision(self, decision, executed: bool, params_snapshot: dict) -> str:
        """
        Registra una decisión (con o sin ejecución). Devuelve el id para luego
        poder llamar `attach_outcome(id, ...)`.
        """
        entry_id = uuid.uuid4().hex[:12]
        entry = {
            "id":              entry_id,
            "timestamp":       decision.timestamp,
            "agent":           decision.agent_name,
            "symbol":          decision.symbol,
            "action":          decision.action.value if hasattr(decision.action, "value") else str(decision.action),
            "confidence":      decision.confidence,
            "features":        decision.features,
            "reasoning":       decision.reasoning,
            "sl_pct":          decision.sl_pct,
            "tp_pct":          decision.tp_pct,
            "executed":        executed,
            "params_snapshot": params_snapshot,
            "outcome":         None,
        }
        with self._lock:
            self._cache.append(entry)
            if len(self._cache) > MAX_ENTRIES:
                # Drop oldest
                self._cache = self._cache[-MAX_ENTRIES:]
                self._rewrite(self._cache)
            else:
                try:
                    with open(self.path, "a") as f:
                        f.write(json.dumps(entry) + "\n")
                except Exception as e:
                    logger.warning(f"ReplayBuffer append failed: {e}")
        return entry_id

    def attach_outcome(self, entry_id: str, outcome: dict) -> bool:
        """
        Adjunta el outcome (pnl, exit_reason, hold_min, etc.) a una decisión previa.
        Re-escribe el archivo (es O(n) pero JSONL queries son raras).
        """
        with self._lock:
            updated = False
            for e in self._cache:
                if e["id"] == entry_id:
                    e["outcome"] = outcome
                    updated = True
                    break
            if updated:
                try:
                    self._rewrite(self._cache)
                except Exception as e:
                    logger.warning(f"attach_outcome rewrite failed: {e}")
            return updated

    # ─── Query helpers (used by agents for cross-learning) ──────────────────

    def all(self) -> List[dict]:
        with self._lock:
            return list(self._cache)

    def recent_closed_for_agent(self, agent_name: str, limit: int = 20) -> List[dict]:
        """Last N closed trades for a given agent (with outcome attached)."""
        with self._lock:
            closed = [e for e in self._cache if e.get("agent") == agent_name and e.get("outcome")]
            return closed[-limit:]

    def recent_closed_excluding(self, agent_name: str, limit: int = 20) -> List[dict]:
        """Last N closed trades from agents OTHER than `agent_name` — used for cross-learning."""
        with self._lock:
            closed = [e for e in self._cache if e.get("agent") != agent_name and e.get("outcome")]
            return closed[-limit:]

    def recent_closed_all(self, limit: int = 50) -> List[dict]:
        with self._lock:
            closed = [e for e in self._cache if e.get("outcome")]
            return closed[-limit:]

    def get_by_id(self, entry_id: str) -> Optional[dict]:
        with self._lock:
            for e in self._cache:
                if e["id"] == entry_id:
                    return e
        return None

    def stats_per_agent(self) -> Dict[str, dict]:
        """Aggregate stats per agent (used by Tournament + dashboard)."""
        with self._lock:
            by_agent: Dict[str, List[dict]] = {}
            for e in self._cache:
                if not e.get("outcome"):
                    continue
                by_agent.setdefault(e["agent"], []).append(e)
        out = {}
        for agent, entries in by_agent.items():
            pnls = [e["outcome"].get("pnl_usdt", 0.0) for e in entries]
            pcts = [e["outcome"].get("pnl_pct", 0.0) for e in entries]
            wins = sum(1 for p in pnls if p > 0)
            n    = len(entries)
            avg_pnl = sum(pnls) / n if n else 0.0
            avg_pct = sum(pcts) / n if n else 0.0
            # Sharpe-like: avg / std * sqrt(n)
            import math
            mean = avg_pct
            var  = sum((x - mean) ** 2 for x in pcts) / n if n > 0 else 0.0
            std  = math.sqrt(var) if var > 0 else 1.0
            sharpe = (mean / std) * math.sqrt(n) if std > 0 else 0.0
            out[agent] = {
                "trades":      n,
                "wins":        wins,
                "losses":      n - wins,
                "win_rate":    round(wins / n * 100.0, 1) if n else 0.0,
                "total_pnl":   round(sum(pnls), 4),
                "avg_pnl":     round(avg_pnl, 4),
                "avg_pnl_pct": round(avg_pct, 3),
                "sharpe":      round(sharpe, 3),
            }
        return out
