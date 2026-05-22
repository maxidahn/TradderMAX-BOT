"""
Tournament — Aprendizaje cruzado entre agentes
================================================
Cada N trades cerrados (o cada M horas), evalúa qué agente está rindiendo mejor
y "coloniza" parámetros del ganador hacia el perdedor.

Mecanismo (algoritmo genético simple):

  1. Calcula Sharpe-like score por agente: (avg_pnl_pct / std) * sqrt(n)
  2. Compara: si ganador tiene > min_edge_pct mejor que perdedor, hace crossover
  3. Crossover: new_param[i] = winner_weight * winner[i] + (1 - winner_weight) * loser[i]
  4. Mutation: a cada parámetro, aplica ruido aleatorio ±mutation_rate
  5. Clamp: cada parámetro respeta rangos seguros (no se puede ir a valores absurdos)

Si ambos van mal (Sharpe<0 sostenido), entra modo defensivo: leverage 1x, sizing 50%.
Si ambos van bien (Sharpe>1 7 días), leverage puede subir +0.5x (cap = max_leverage).

Resultado del torneo se persiste en data/tournament_log.json (auditoría).
"""

import json
import logging
import os
import random
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Dict, List, Optional

logger = logging.getLogger("celerity.tournament")

DATA_DIR = os.getenv("DATA_DIR", "data")
TOURNAMENT_LOG = os.path.join(DATA_DIR, "tournament_log.json")
AGENT_PARAMS_FILE = os.path.join(DATA_DIR, "agent_params.json")

# Rangos seguros por parámetro — el clamping evita que un crossover loco rompa los agentes
PARAM_BOUNDS = {
    "min_confidence":        (0.30, 0.85),
    "sl_pct":                (0.5, 4.0),
    "tp_pct":                (1.0, 8.0),
    "max_hold_minutes":      (30, 480),
    "trailing_after_pct":    (0.5, 3.0),
    "trailing_distance_pct": (0.3, 2.0),
    "ema_fast":              (5, 20),
    "ema_slow":              (15, 50),
    "adx_min":               (15.0, 40.0),
    "volume_min_ratio":      (0.5, 2.5),
    "rsi_extreme_low":       (15.0, 35.0),
    "rsi_extreme_high":      (65.0, 85.0),
    "bb_period":             (10, 30),
    "bb_std":                (1.5, 3.0),
    "funding_extreme":       (0.01, 0.10),
}


def _clamp_param(name: str, value):
    """Recorta el valor al rango permitido para ese parámetro."""
    bounds = PARAM_BOUNDS.get(name)
    if not bounds:
        return value
    lo, hi = bounds
    if isinstance(value, int):
        return int(max(lo, min(hi, value)))
    return float(max(lo, min(hi, value)))


class Tournament:

    def __init__(self, agents_config, replay_buffer, agents: list, bandit=None):
        """
        agents_config: AgentsConfig instance
        replay_buffer: ReplayBuffer instance
        agents: lista de BaseAgent instances (los participantes del torneo)
        bandit: BanditPool opcional — si está, también evalúa promoción de variantes
        """
        self.cfg = agents_config
        self.replay = replay_buffer
        self.agents = agents
        self.bandit = bandit
        self.last_run_ts = 0.0
        self.last_total_trades = 0
        self._load_persisted_params()

    def _load_persisted_params(self):
        """Si hay parámetros persistidos de runs anteriores, los aplica a los agentes."""
        if not os.path.exists(AGENT_PARAMS_FILE):
            return
        try:
            with open(AGENT_PARAMS_FILE) as f:
                data = json.load(f)
            for agent in self.agents:
                if agent.name in data:
                    saved = data[agent.name]
                    # Apply only known fields
                    for key, val in saved.items():
                        if hasattr(agent.params, key):
                            setattr(agent.params, key, val)
                    logger.info(f"[Tournament] restored params for {agent.name}")
        except Exception as e:
            logger.warning(f"[Tournament] could not load persisted params: {e}")

    def _save_params(self):
        try:
            data = {a.name: asdict(a.params) for a in self.agents}
            with open(AGENT_PARAMS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"[Tournament] save params failed: {e}")

    def _append_log(self, event: dict):
        existing = []
        if os.path.exists(TOURNAMENT_LOG):
            try:
                with open(TOURNAMENT_LOG) as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.append({**event, "timestamp": datetime.now(timezone.utc).isoformat()})
        # Keep last 200 events
        existing = existing[-200:]
        try:
            with open(TOURNAMENT_LOG, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.warning(f"[Tournament] log write failed: {e}")

    def should_run(self) -> bool:
        """¿Toca correr el torneo (por N trades o M horas)?"""
        import time
        stats = self.replay.stats_per_agent()
        total_trades = sum(s["trades"] for s in stats.values())

        # Trigger condition 1: enough new trades since last run
        new_trades = total_trades - self.last_total_trades
        if new_trades >= self.cfg.tournament_every_n_trades:
            return True

        # Trigger condition 2: enough time elapsed since last run (and at least some trades)
        elapsed_h = (time.time() - self.last_run_ts) / 3600.0
        if elapsed_h >= self.cfg.tournament_every_hours and total_trades > 0:
            return True

        return False

    def run(self) -> dict:
        """
        Corre un torneo: compara agentes y aplica crossover si corresponde.
        Si hay BanditPool inyectado, también evalúa promoción de variantes
        y descarte de underperformers.
        Devuelve dict resumen del evento.
        """
        import time
        stats = self.replay.stats_per_agent()
        self.last_run_ts = time.time()
        self.last_total_trades = sum(s["trades"] for s in stats.values())

        # ── Bandit pre-step: promotion + cull ────────────────────────────────
        bandit_actions = []
        if self.bandit:
            try:
                # Cull variantes con sharpe muy negativo (las reemplaza por nuevas)
                self.bandit.cull_underperformers()

                # Check si alguna variante supera al principal en paper sintético
                promotions = self.bandit.check_promotion()
                for promo in promotions:
                    principal_name = promo["principal"]
                    principal_sharpe = stats.get(principal_name, {}).get("sharpe", 0.0)
                    variant_sharpe = promo["variant_sharpe"]
                    # Solo promover si la variante supera al principal por
                    # bandit_promotion_min_edge_pct
                    if principal_sharpe <= 0:
                        edge_pct = 100.0 if variant_sharpe > 0 else 0
                    else:
                        edge_pct = (variant_sharpe - principal_sharpe) / principal_sharpe * 100
                    if edge_pct >= self.cfg.bandit_promotion_min_edge_pct:
                        ok = self.bandit.promote(principal_name, promo["variant"])
                        if ok:
                            bandit_actions.append({
                                "action":   "promotion",
                                "principal": principal_name,
                                "variant":  promo["variant"],
                                "edge_pct": round(edge_pct, 1),
                            })
                            logger.info(
                                f"[Tournament+Bandit] PROMOTED {promo['variant']} → "
                                f"{principal_name} (edge {edge_pct:.0f}%)"
                            )
            except Exception as e:
                logger.warning(f"[Tournament] bandit step failed: {e}")

        # Filter agents with enough trades
        eligible = [
            a for a in self.agents
            if stats.get(a.name, {}).get("trades", 0) >= self.cfg.min_trades_for_tournament
        ]
        if len(eligible) < 2:
            event = {
                "event":          "skipped",
                "reason":         f"not enough eligible agents (need {self.cfg.min_trades_for_tournament}+ trades each)",
                "stats":          stats,
                "bandit_actions": bandit_actions,
            }
            self._append_log(event)
            return event

        # Rank by Sharpe
        ranked = sorted(eligible, key=lambda a: stats[a.name]["sharpe"], reverse=True)
        winner = ranked[0]
        loser  = ranked[-1]
        win_sharpe = stats[winner.name]["sharpe"]
        lose_sharpe = stats[loser.name]["sharpe"]

        # ── Caso A: ambos van mal → modo defensivo ───────────────────────────
        if win_sharpe < 0 and lose_sharpe < 0:
            event = {
                "event":          "defensive_mode",
                "reason":         f"both agents Sharpe<0 (winner: {win_sharpe:.2f}, loser: {lose_sharpe:.2f})",
                "stats":          stats,
                "applied":        "min_confidence raised to 0.70, sl tightened",
                "bandit_actions": bandit_actions,
            }
            for a in self.agents:
                a.params.min_confidence = max(a.params.min_confidence, 0.70)
                a.params.sl_pct = min(a.params.sl_pct, 1.2)
            self._save_params()
            self._append_log(event)
            logger.warning(f"[Tournament] DEFENSIVE MODE — both agents underperforming")
            return event

        # ── Caso B: edge insuficiente → no hacer nada ────────────────────────
        if win_sharpe <= 0 or lose_sharpe == 0:
            edge_pct = 0
        else:
            # Edge: cuánto mejor es ganador vs perdedor (en términos de Sharpe normalizado)
            edge_pct = ((win_sharpe - lose_sharpe) / max(abs(lose_sharpe), 0.01)) * 100

        if winner.name == loser.name or edge_pct < self.cfg.tournament_min_edge_pct:
            event = {
                "event":          "no_action",
                "reason":         f"edge {edge_pct:.0f}% < threshold {self.cfg.tournament_min_edge_pct}%",
                "winner":         winner.name,
                "loser":          loser.name,
                "stats":          stats,
                "bandit_actions": bandit_actions,
            }
            self._append_log(event)
            return event

        # ── Caso C: crossover ────────────────────────────────────────────────
        new_params = self._crossover(
            winner_params=winner.params,
            loser_params=loser.params,
            winner_weight=self.cfg.crossover_winner_weight,
            mutation_rate=self.cfg.mutation_rate,
        )
        loser.update_params(new_params)
        self._save_params()

        event = {
            "event":            "crossover",
            "winner":           winner.name,
            "loser":            loser.name,
            "winner_sharpe":    round(win_sharpe, 3),
            "loser_sharpe":     round(lose_sharpe, 3),
            "edge_pct":         round(edge_pct, 1),
            "winner_weight":    self.cfg.crossover_winner_weight,
            "mutation_rate":    self.cfg.mutation_rate,
            "new_loser_params": asdict(new_params),
            "stats":            stats,
            "bandit_actions":   bandit_actions,
        }
        self._append_log(event)
        logger.info(
            f"[Tournament] CROSSOVER — {winner.name} (Sharpe {win_sharpe:.2f}) "
            f"colonized {loser.name} (Sharpe {lose_sharpe:.2f}) — edge {edge_pct:.0f}%"
        )
        return event

    def _crossover(self, winner_params, loser_params, winner_weight: float, mutation_rate: float):
        """
        Mezcla parámetros con un peso (60/40 por default) y aplica mutación gaussiana.
        Devuelve un nuevo AgentParams (mismo type que loser_params).
        """
        from copy import deepcopy
        out = deepcopy(loser_params)

        for field_name in out.__dataclass_fields__:
            wval = getattr(winner_params, field_name)
            lval = getattr(loser_params, field_name)

            # Solo cruzamos numéricos
            if isinstance(wval, (int, float)) and isinstance(lval, (int, float)):
                blended = winner_weight * wval + (1.0 - winner_weight) * lval
                # Mutación: ±mutation_rate de ruido relativo
                mutation = blended * random.uniform(-mutation_rate, mutation_rate)
                final = blended + mutation
                # Preserve int type if original was int
                if isinstance(lval, int) and not isinstance(lval, bool):
                    final = int(round(final))
                final = _clamp_param(field_name, final)
                setattr(out, field_name, final)
        return out

    def get_leaderboard(self) -> List[dict]:
        """Para el dashboard."""
        stats = self.replay.stats_per_agent()
        rows = []
        for agent in self.agents:
            s = stats.get(agent.name, {})
            rows.append({
                "agent":       agent.name,
                "trades":      s.get("trades", 0),
                "win_rate":    s.get("win_rate", 0.0),
                "total_pnl":   s.get("total_pnl", 0.0),
                "avg_pnl_pct": s.get("avg_pnl_pct", 0.0),
                "sharpe":      s.get("sharpe", 0.0),
                "params":      asdict(agent.params),
            })
        return sorted(rows, key=lambda r: r["sharpe"], reverse=True)

    def get_recent_events(self, limit: int = 20) -> List[dict]:
        if not os.path.exists(TOURNAMENT_LOG):
            return []
        try:
            with open(TOURNAMENT_LOG) as f:
                data = json.load(f)
            return data[-limit:]
        except Exception:
            return []
