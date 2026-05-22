"""
Bandit Pool — Exploración paralela de variantes en paper sintético
=====================================================================
Mantiene una población de "variantes" (clones mutados) por cada agente principal.
Las variantes deciden sobre los mismos candles que el principal pero **NO ejecutan
en live** — su outcome se simula localmente (paper sintético).

Por cada tick:
  - El principal decide y eventualmente ejecuta una posición real.
  - Cada variante también decide; si su action != FLAT, se "abre" una posición
    sintética que se evalúa contra los candles futuros (precio entry + SL/TP fijo).

Cada variante mantiene:
  - n_trades_synthetic (cuántas decisiones se cerraron en paper sintético)
  - sum_pnl_pct (PnL acumulado en paper)
  - sharpe_synthetic (Sharpe-like sobre el paper)

Cuando una variante supera al principal por >bandit_promotion_min_edge_pct
(con al menos bandit_promotion_min_trades), el orchestrator/tournament puede
PROMOVERLA: los parámetros de la variante reemplazan al principal.

Thompson sampling-ish: cuando hay que matar una variante mala, se reemplaza
muestreando sus reemplazos con probabilidad ∝ exp(sharpe_promedio).
"""

import logging
import math
import random
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("celerity.bandit")


# ─── Helpers reused from tournament for parameter bounds ─────────────────────
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


def _clamp(name: str, value):
    bounds = PARAM_BOUNDS.get(name)
    if not bounds:
        return value
    lo, hi = bounds
    if isinstance(value, int) and not isinstance(value, bool):
        return int(max(lo, min(hi, value)))
    return float(max(lo, min(hi, value)))


def _mutate_params(base_params, mutation_rate: float):
    """Devuelve una copia profundamente mutada. mutation_rate aplica a cada campo numérico."""
    out = deepcopy(base_params)
    for name in out.__dataclass_fields__:
        val = getattr(out, name)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            noise = val * random.uniform(-mutation_rate, mutation_rate)
            new = val + noise
            if isinstance(val, int):
                new = int(round(new))
            setattr(out, name, _clamp(name, new))
    return out


@dataclass
class SyntheticPosition:
    """Una posición abierta solo en paper sintético (no en Binance)."""
    symbol: str
    side: str           # 'LONG' / 'SHORT'
    entry_price: float
    entry_idx: int      # Índice de la vela de entrada
    sl_price: float
    tp_price: float
    max_hold_minutes: int


@dataclass
class VariantStats:
    """Métricas de una variante."""
    name: str
    parent: str
    n_trades_synthetic: int = 0
    n_wins: int = 0
    sum_pnl_pct: float = 0.0
    sum_pnl_pct_sq: float = 0.0
    last_pnl_pcts: List[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def record(self, pnl_pct: float):
        self.n_trades_synthetic += 1
        if pnl_pct > 0:
            self.n_wins += 1
        self.sum_pnl_pct += pnl_pct
        self.sum_pnl_pct_sq += pnl_pct ** 2
        self.last_pnl_pcts.append(pnl_pct)
        if len(self.last_pnl_pcts) > 50:
            self.last_pnl_pcts = self.last_pnl_pcts[-50:]

    @property
    def avg_pnl_pct(self) -> float:
        return self.sum_pnl_pct / self.n_trades_synthetic if self.n_trades_synthetic > 0 else 0.0

    @property
    def std_pnl_pct(self) -> float:
        if self.n_trades_synthetic < 2:
            return 1.0
        mean = self.avg_pnl_pct
        var = (self.sum_pnl_pct_sq / self.n_trades_synthetic) - mean ** 2
        return math.sqrt(max(var, 1e-6))

    @property
    def sharpe(self) -> float:
        if self.n_trades_synthetic < 3:
            return 0.0
        return (self.avg_pnl_pct / self.std_pnl_pct) * math.sqrt(self.n_trades_synthetic)

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades_synthetic * 100.0 if self.n_trades_synthetic > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "name":         self.name,
            "parent":       self.parent,
            "trades":       self.n_trades_synthetic,
            "wins":         self.n_wins,
            "win_rate":     round(self.win_rate, 1),
            "avg_pnl_pct":  round(self.avg_pnl_pct, 3),
            "sharpe":       round(self.sharpe, 3),
            "age_minutes":  int((time.time() - self.created_at) / 60),
        }


class Variant:
    """Un clone mutado de un agente principal. NO ejecuta en live."""

    def __init__(self, name: str, parent_name: str, agent_class, mutated_params,
                  perpetuals_data=None):
        self.name = name
        self.parent_name = parent_name
        # Instancia un nuevo agente con los params mutados (sin online_ml/contagion para no contaminar)
        self.agent = agent_class(params=mutated_params, perpetuals_data=perpetuals_data, replay_buffer=None)
        # Override del nombre para que en logs se distinga
        self.agent.name = name
        self.stats = VariantStats(name=name, parent=parent_name)
        self.open_positions: Dict[str, SyntheticPosition] = {}

    def decide_and_record(self, symbol: str, candles: pd.DataFrame, current_idx: int):
        """
        Pide decisión y, si es LONG/SHORT y no hay posición abierta, abre una sintética.
        También evalúa posiciones abiertas contra el último precio.
        """
        # 1. Evaluar posiciones abiertas sintéticas → pueden cerrar por SL/TP/timeout
        self._evaluate_open_positions(symbol, candles, current_idx)

        # 2. Pedir decisión
        from agents.base_agent import Action
        try:
            decision = self.agent.decide(symbol, candles)
        except Exception as e:
            logger.debug(f"[Bandit/{self.name}] decide error {symbol}: {e}")
            return

        # Skip si FLAT, sin confidence suficiente, o ya tenemos posición en el símbolo
        if decision.action == Action.FLAT:
            return
        if symbol in self.open_positions:
            return
        if decision.confidence < self.agent.params.min_confidence:
            return

        # 3. Abrir posición sintética
        try:
            entry_price = float(candles["close"].iloc[-1])
            sl_pct = decision.sl_pct or self.agent.params.sl_pct
            tp_pct = decision.tp_pct or self.agent.params.tp_pct
            if decision.action == Action.LONG:
                sl_price = entry_price * (1 - sl_pct / 100.0)
                tp_price = entry_price * (1 + tp_pct / 100.0)
            else:
                sl_price = entry_price * (1 + sl_pct / 100.0)
                tp_price = entry_price * (1 - tp_pct / 100.0)
            self.open_positions[symbol] = SyntheticPosition(
                symbol=symbol,
                side=decision.action.value,
                entry_price=entry_price,
                entry_idx=current_idx,
                sl_price=sl_price,
                tp_price=tp_price,
                max_hold_minutes=self.agent.params.max_hold_minutes,
            )
        except Exception as e:
            logger.debug(f"[Bandit/{self.name}] could not open synthetic {symbol}: {e}")

    def _evaluate_open_positions(self, symbol: str, candles: pd.DataFrame, current_idx: int):
        """Chequea SL/TP/timeout contra el último candle."""
        pos = self.open_positions.get(symbol)
        if not pos:
            return
        try:
            last_high  = float(candles["high"].iloc[-1])
            last_low   = float(candles["low"].iloc[-1])
            last_close = float(candles["close"].iloc[-1])
        except Exception:
            return

        closed = False
        pnl_pct = 0.0

        if pos.side == "LONG":
            if last_low <= pos.sl_price:
                pnl_pct = (pos.sl_price - pos.entry_price) / pos.entry_price * 100.0
                closed = True
            elif last_high >= pos.tp_price:
                pnl_pct = (pos.tp_price - pos.entry_price) / pos.entry_price * 100.0
                closed = True
        else:  # SHORT
            if last_high >= pos.sl_price:
                pnl_pct = (pos.entry_price - pos.sl_price) / pos.entry_price * 100.0
                closed = True
            elif last_low <= pos.tp_price:
                pnl_pct = (pos.entry_price - pos.tp_price) / pos.entry_price * 100.0
                closed = True

        # Timeout: candles 5m → max_hold_minutes / 5 candles aprox
        candles_held = current_idx - pos.entry_idx
        max_candles = pos.max_hold_minutes // 5
        if not closed and candles_held >= max_candles:
            # Cierre por timeout — PnL al precio actual
            if pos.side == "LONG":
                pnl_pct = (last_close - pos.entry_price) / pos.entry_price * 100.0
            else:
                pnl_pct = (pos.entry_price - last_close) / pos.entry_price * 100.0
            closed = True

        if closed:
            # Restar fees aproximadas (taker 0.04% x 2 = 0.08%)
            pnl_pct -= 0.08
            self.stats.record(pnl_pct)
            del self.open_positions[symbol]
            logger.debug(
                f"[Bandit/{self.name}] synthetic closed {pos.side} {symbol} pnl={pnl_pct:+.2f}% "
                f"(trades={self.stats.n_trades_synthetic}, sharpe={self.stats.sharpe:.2f})"
            )


class BanditPool:
    """Una pool de variantes por cada agente principal."""

    def __init__(self, agents_config, principals: list, agent_class_map: dict,
                  perpetuals_data=None):
        """
        principals: lista de BaseAgent (los 2 principales)
        agent_class_map: {agent_name: AgentClass} — para instanciar variantes
        """
        self.cfg = agents_config
        self.principals = principals
        self.agent_class_map = agent_class_map
        self.perpetuals_data = perpetuals_data
        # Pool por agente_name → List[Variant]
        self.variants: Dict[str, List[Variant]] = {a.name: [] for a in principals}
        # Tick counter para llevar índice de candles
        self._tick_count = 0
        # Inicializar pool con variantes mutadas
        self._initialize_pool()

    def _initialize_pool(self):
        """Crea n variantes por agente, mutadas a partir de los params iniciales.

        2026-05-19 NUCLEAR: el Momentum estuvo perdiendo plata en TODAS sus
        variantes (Sharpe -2.5 a -4.9). Le damos mutación amplia (40%) para que
        explore un espacio de parámetros muy distinto al principal.
        El Sniper conserva su mutación normal (25%) — funciona bien tal cual."""
        n = self.cfg.bandit_variants_per_agent
        # Per-agent mutation rate (override mode tras un reset nuclear)
        MUTATION_BY_AGENT = {
            "MomentumHunter":  0.40,  # exploración amplia, mercado bear no apto para este filosof
            "ReversalSniper":  0.25,  # default — funciona
        }
        for principal in self.principals:
            cls = self.agent_class_map.get(principal.name)
            if not cls:
                continue
            mut_rate = MUTATION_BY_AGENT.get(principal.name, 0.25)
            for i in range(n):
                mutated = _mutate_params(principal.params, mutation_rate=mut_rate)
                variant_name = f"{principal.name}_v{i+1}"
                v = Variant(variant_name, principal.name, cls, mutated, perpetuals_data=self.perpetuals_data)
                self.variants[principal.name].append(v)
                logger.info(f"[Bandit] spawned variant {variant_name} (mutation_rate={mut_rate})")

    def tick(self, symbol: str, candles: pd.DataFrame):
        """Llamado por orchestrator en cada tick. Cada variante decide + evalúa."""
        self._tick_count += 1
        for principal in self.principals:
            for variant in self.variants.get(principal.name, []):
                variant.decide_and_record(symbol, candles, self._tick_count)

    def check_promotion(self) -> List[dict]:
        """
        Para cada agente, busca si alguna variante supera al principal por
        bandit_promotion_min_edge_pct con al menos bandit_promotion_min_trades.
        Devuelve lista de promociones aplicadas (puede estar vacía).
        """
        promotions = []
        for principal in self.principals:
            variants = self.variants.get(principal.name, [])
            if not variants:
                continue

            # Sharpe del principal viene del replay buffer (lo calcula tournament).
            # Aquí no tenemos acceso directo — pero el llamador (tournament) puede pasarlo.
            # Por ahora identificamos variantes elegibles y devolvemos info.
            eligible = [
                v for v in variants
                if v.stats.n_trades_synthetic >= self.cfg.bandit_promotion_min_trades
                and v.stats.sharpe > 0
            ]
            if not eligible:
                continue
            best = max(eligible, key=lambda v: v.stats.sharpe)
            promotions.append({
                "principal":   principal.name,
                "variant":     best.name,
                "variant_sharpe": round(best.stats.sharpe, 3),
                "variant_trades": best.stats.n_trades_synthetic,
                "variant_win_rate": round(best.stats.win_rate, 1),
                "params":      asdict(best.agent.params),
            })
        return promotions

    def promote(self, principal_name: str, variant_name: str):
        """
        Promociona una variante: sus parámetros reemplazan al principal,
        y la pool de variantes de ese agente se reinicializa con mutaciones del nuevo principal.
        """
        principal = next((a for a in self.principals if a.name == principal_name), None)
        if not principal:
            return False
        variants = self.variants.get(principal_name, [])
        variant = next((v for v in variants if v.name == variant_name), None)
        if not variant:
            return False

        # Reemplazar params del principal
        new_params = deepcopy(variant.agent.params)
        principal.update_params(new_params)

        # Reset pool: nuevas variantes mutadas del nuevo principal
        cls = self.agent_class_map.get(principal_name)
        if cls:
            self.variants[principal_name] = []
            for i in range(self.cfg.bandit_variants_per_agent):
                mutated = _mutate_params(new_params, mutation_rate=0.20)
                new_variant_name = f"{principal_name}_v{i+1}"
                v = Variant(new_variant_name, principal_name, cls, mutated,
                              perpetuals_data=self.perpetuals_data)
                self.variants[principal_name].append(v)

        logger.info(f"[Bandit] PROMOTED {variant_name} → {principal_name} (pool reset)")
        return True

    def cull_underperformers(self):
        """Mata variantes con muchos trades + Sharpe muy negativo, las reemplaza."""
        for principal in self.principals:
            variants = self.variants.get(principal.name, [])
            for i, v in enumerate(list(variants)):
                if v.stats.n_trades_synthetic >= 15 and v.stats.sharpe < -1.0:
                    cls = self.agent_class_map.get(principal.name)
                    if not cls:
                        continue
                    # Replace by a new mutation of the principal (no de la variante mala)
                    new_params = _mutate_params(principal.params, mutation_rate=0.25)
                    new_name = f"{principal.name}_v{i+1}"
                    new_v = Variant(new_name, principal.name, cls, new_params,
                                     perpetuals_data=self.perpetuals_data)
                    variants[i] = new_v
                    logger.info(
                        f"[Bandit] culled {v.name} (Sharpe {v.stats.sharpe:.2f}) "
                        f"→ respawned as {new_name}"
                    )

    def get_status(self) -> dict:
        out = {}
        for principal in self.principals:
            variants = self.variants.get(principal.name, [])
            out[principal.name] = [v.stats.to_dict() for v in variants]
        return out
