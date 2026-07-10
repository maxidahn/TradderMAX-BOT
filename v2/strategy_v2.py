"""
Celerity v2 — Estrategia (versión LOCAL)
=========================================
Misma lógica que el Pine Script, pero calculada por el bot para correr sin
TradingView. Trend-following long+short sobre velas cerradas:

  Régimen:  Close > EMA200 → solo LONG ;  Close < EMA200 → solo SHORT
  Entrada LONG:   EMA20 > EMA50  y  Close rompe máximo Donchian(20) previo
  Entrada SHORT:  EMA20 < EMA50  y  Close rompe mínimo  Donchian(20) previo
  Salida LONG:    Close < EMA50  o  Close < Chandelier_long  (máx22 − 3·ATR)
  Salida SHORT:   Close > EMA50  o  Close > Chandelier_short (mín22 + 3·ATR)

`decide()` es una función PURA: recibe las velas y el lado actual, devuelve
qué hacer. No toca la red ni el estado.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class Params:
    ema_trend: int = 200
    ema_fast: int = 20
    ema_slow: int = 50
    donchian: int = 20
    atr_len: int = 14
    chand_len: int = 22
    chand_mult: float = 3.0


@dataclass
class Decision:
    action: Optional[str]   # "LONG" | "SHORT" | "CLOSE" | None
    sl_pct: float           # distancia de stop en %, para el sizing
    reason: str


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def decide(df: pd.DataFrame, current_side: Optional[str], p: Params = Params()) -> Decision:
    """
    df: velas con columnas open/high/low/close/volume, la ÚLTIMA debe ser una vela
        YA CERRADA (run_local descarta la vela en formación antes de llamar).
    current_side: "LONG" | "SHORT" | None (posición abierta hoy en ese símbolo)
    """
    if df is None or len(df) < p.ema_trend + 5:
        return Decision(None, 0.0, "sin datos suficientes")

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    ema_t = _ema(close, p.ema_trend).iloc[-1]
    ema_f = _ema(close, p.ema_fast).iloc[-1]
    ema_s = _ema(close, p.ema_slow).iloc[-1]
    atr = _atr(df, p.atr_len).iloc[-1]

    price = close.iloc[-1]
    donch_hi = high.iloc[-(p.donchian + 1):-1].max()   # máximo previo (sin vela actual)
    donch_lo = low.iloc[-(p.donchian + 1):-1].min()
    chand_long = high.iloc[-p.chand_len:].max() - p.chand_mult * atr
    chand_short = low.iloc[-p.chand_len:].min() + p.chand_mult * atr

    sl_pct = round(p.chand_mult * atr / price * 100, 2) if price > 0 else 3.0

    regime_up = price > ema_t
    regime_down = price < ema_t
    long_setup = regime_up and ema_f > ema_s and price > donch_hi
    short_setup = regime_down and ema_f < ema_s and price < donch_lo
    exit_long = price < ema_s or price < chand_long
    exit_short = price > ema_s or price > chand_short

    # ── Ya en posición: ¿salir o revertir? ──────────────────────────────────
    if current_side == "LONG":
        if short_setup:
            return Decision("SHORT", sl_pct, "reversión: setup short con marea bajista")
        if exit_long:
            return Decision("CLOSE", 0.0, "salida long: Close<EMA50 o Chandelier")
        return Decision(None, 0.0, "mantener long")

    if current_side == "SHORT":
        if long_setup:
            return Decision("LONG", sl_pct, "reversión: setup long con marea alcista")
        if exit_short:
            return Decision("CLOSE", 0.0, "salida short: Close>EMA50 o Chandelier")
        return Decision(None, 0.0, "mantener short")

    # ── Sin posición: ¿entrar? ──────────────────────────────────────────────
    if long_setup:
        return Decision("LONG", sl_pct, f"LONG: régimen↑, EMA20>50, ruptura {donch_hi:.4f}")
    if short_setup:
        return Decision("SHORT", sl_pct, f"SHORT: régimen↓, EMA20<50, ruptura {donch_lo:.4f}")
    return Decision(None, 0.0, "sin setup (esperando)")
