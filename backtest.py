"""
Celerity Trader — Backtest A/B de filtros de entrada
=====================================================
Mide el efecto de las mejoras aplicadas (anti-chop ADX + gate de coste +
cooldown post-stop-loss + nivel de riesgo) sobre el núcleo TÉCNICO de la
estrategia, descargando histórico real de Binance.

  python backtest.py                 # 30 días, todos los pares activos
  python backtest.py --days 60       # 60 días
  python backtest.py --symbol SOLUSDC

IMPORTANTE — qué mide y qué no:
  - Modela la capa TÉCNICA (EMA/RSI/Volumen + extensión/pullback), que es el
    driver dominante de las señales, MÁS los filtros nuevos. NO incluye las
    capas auxiliares ML / sentimiento / Claude / TradingView (esas no se pueden
    reproducir de forma determinista offline). Por eso los números absolutos no
    serán idénticos al bot en vivo; lo relevante es la COMPARACIÓN A vs B.
  - Fees y slippage se aplican como en producción.

Requiere: BINANCE_API_KEY / BINANCE_API_SECRET en el entorno o .env
(solo lectura de klines; no coloca órdenes).
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from binance.client import Client

from config import config
from strategy import (
    calculate_ema, calculate_rsi, calculate_volume_ratio,
    calculate_adx, calculate_atr,
)


# ───────────────────────── Descarga de histórico ──────────────────────────
def fetch_klines(client: Client, symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    candles_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24}.get(interval, 288)
    total = candles_per_day * days
    rows = []
    end = None
    try:
        while len(rows) < total:
            batch = client.get_klines(symbol=symbol, interval=interval, limit=1000, endTime=end)
            if not batch:
                break
            rows = batch + rows
            end = batch[0][0] - 1
            if len(batch) < 1000:
                break
    except Exception as e:
        print(f"  [!] {symbol}: error descargando klines: {e}")
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "ct", "qv", "trades", "tbb", "tbq", "ig"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


# ───────────────────── Score técnico (espejo de strategy._analyze_technical) ─────────────────────
def technical_score(window: pd.DataFrame, cfg) -> float:
    close = window["close"]
    volume = window["volume"]
    ema_fast = calculate_ema(close, cfg.ema_fast)
    ema_slow = calculate_ema(close, cfg.ema_slow)
    rsi = calculate_rsi(close, cfg.rsi_period)
    vol_ratio = calculate_volume_ratio(volume, cfg.volume_ma_period)

    price = close.iloc[-1]
    cur_rsi = rsi.iloc[-1] if not np.isnan(rsi.iloc[-1]) else 50.0
    cf, cs = ema_fast.iloc[-1], ema_slow.iloc[-1]
    cvr = vol_ratio.iloc[-1] if not np.isnan(vol_ratio.iloc[-1]) else 1.0
    pf, ps = ema_fast.iloc[-2], ema_slow.iloc[-2]

    bull_cross = (pf <= ps) and (cf > cs)
    bear_cross = (pf >= ps) and (cf < cs)
    ema_bull = cf > cs
    vol_ok = cvr >= cfg.volume_threshold
    spread = ((cf - cs) / cs) * 100
    rsi_prev3 = rsi.iloc[-4:-1].mean() if len(rsi) >= 4 else rsi.iloc[-2]
    rsi_mom = cur_rsi - rsi_prev3
    p_above_f = price > cf
    p_above_s = price > cs

    score = 0.0
    ext_cross = (price - cf) / cf * 100
    if bull_cross:
        score += 0.20 if ext_cross > 0.8 else 0.50
    elif bear_cross:
        score -= 0.50
    else:
        if ema_bull:
            score += 0.15 + min(spread / 1.0 * 0.20, 0.20)
        else:
            score -= 0.15 + abs(max(spread / 1.0 * 0.20, -0.20))

    if p_above_f and p_above_s:
        score += 0.08
    elif not p_above_f and not p_above_s:
        score -= 0.08

    if cur_rsi < cfg.rsi_oversold:
        score += 0.30
    elif cur_rsi > cfg.rsi_overbought:
        score -= 0.30
    else:
        if abs(rsi_mom) > 3:
            score += min(max(rsi_mom / 25.0 * 0.15, -0.15), 0.15)

    if cvr < 0.30:
        score *= 0.50
    elif vol_ok:
        score *= 1.25

    ext = (price - cf) / cf * 100
    if ema_bull:
        if ext > 1.5:
            score -= 0.25
        elif 0.0 <= ext <= 0.5:
            score += 0.15
    elif ext < -1.5:
        score += 0.25

    if score > 0.10 and rsi_mom < -3:
        score *= 0.65

    return max(-1, min(1, score))


# ───────────────────── Simulador ──────────────────────
@dataclass
class Trade:
    pnl_net: float
    reason: str


def gates_pass(window: pd.DataFrame, cfg, costs) -> bool:
    price = window["close"].iloc[-1]
    if cfg.anti_chop_enabled:
        adx = calculate_adx(window, cfg.adx_period).iloc[-1]
        if not np.isnan(adx) and adx < cfg.adx_min:
            return False
    if cfg.cost_gate_enabled and price > 0:
        atr = calculate_atr(window, cfg.atr_period).iloc[-1]
        if not np.isnan(atr):
            atr_pct = atr / price * 100.0
            rt = costs.round_trip_fee_pct + 2 * costs.slippage_base_pct
            if atr_pct < rt * cfg.cost_gate_atr_mult:
                return False
    return True


def simulate(df: pd.DataFrame, cfg, costs, risk, use_filters: bool, post_sl_cooldown_candles: int) -> List[Trade]:
    """Recorre el histórico vela a vela, abre/cierra una posición a la vez por par."""
    trades: List[Trade] = []
    sl_pct = risk["stop_loss_pct"]
    tp_pct = risk["take_profit_pct"]
    threshold = risk["signal_threshold"]
    trail_pct = cfg.trailing_stop_pct
    fee = costs.effective_fee_rate * 100  # % por lado
    slip = costs.slippage_base_pct        # % por lado

    warmup = max(cfg.ema_slow, cfg.rsi_period, cfg.volume_ma_period, cfg.adx_period, 30) + 5
    pos = None  # dict: entry, peak, bars, partial
    cooldown_until = -1

    for i in range(warmup, len(df) - 1):
        window = df.iloc[: i + 1]
        price = df["close"].iloc[i]
        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        if pos is not None:
            pos["bars"] += 1
            pos["peak"] = max(pos["peak"], high)
            entry = pos["entry"]
            gain_low = (low - entry) / entry * 100
            gain_high = (high - entry) / entry * 100
            peak_gain = (pos["peak"] - entry) / entry * 100

            exit_price = None
            reason = None
            # SL primero (conservador)
            if gain_low <= -sl_pct:
                exit_price = entry * (1 - sl_pct / 100); reason = "STOP_LOSS"
            elif not pos["partial"] and gain_high >= tp_pct / 2:
                # Partial TP: cierra mitad, sigue con el resto
                pnl = _net_pct(tp_pct / 2, fee, slip) * 0.5
                trades.append(Trade(pnl, "PARTIAL_TP"))
                pos["partial"] = True
                if gain_high >= tp_pct:
                    exit_price = entry * (1 + tp_pct / 100); reason = "TAKE_PROFIT"
            elif gain_high >= tp_pct:
                exit_price = entry * (1 + tp_pct / 100); reason = "TAKE_PROFIT"
            elif trail_pct > 0 and peak_gain >= 1.5:
                dd = (pos["peak"] - price) / pos["peak"] * 100
                if dd >= trail_pct:
                    exit_price = price; reason = "TRAILING_STOP"
            if exit_price is None and pos["bars"] >= int(cfg.max_hold_hours * 12):  # 12 velas 5m/hora
                exit_price = price; reason = "TIMEOUT"

            if exit_price is not None:
                gross = (exit_price - entry) / entry * 100
                frac = 0.5 if pos["partial"] else 1.0
                trades.append(Trade(_net_pct(gross, fee, slip) * frac, reason))
                if reason == "STOP_LOSS":
                    cooldown_until = i + post_sl_cooldown_candles
                pos = None
            continue

        # Sin posición → ¿entrada?
        if i < cooldown_until:
            continue
        score = technical_score(window, cfg)
        if score > threshold:
            if use_filters and not gates_pass(window, cfg, costs):
                continue
            pos = {"entry": price, "peak": price, "bars": 0, "partial": False}

    return trades


def _net_pct(gross_pct: float, fee_pct: float, slip_pct: float) -> float:
    """PnL neto en % tras fees (2 lados) y slippage (2 lados)."""
    return gross_pct - 2 * fee_pct - 2 * slip_pct


# ───────────────────── Métricas ──────────────────────
def metrics(trades: List[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    p = [t.pnl_net for t in trades]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    eq = np.cumsum(p)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq)
    return {
        "n": len(p),
        "net_pct": sum(p),
        "win_rate": 100 * len(wins) / len(p),
        "avg_win": (gross_win / len(wins)) if wins else 0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0,
        "expectancy": sum(p) / len(p),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_dd": float(dd.max()) if len(dd) else 0,
    }


def show(label: str, m: dict):
    if m.get("n", 0) == 0:
        print(f"  {label:<16} sin trades")
        return
    pf = m["profit_factor"]
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    print(f"  {label:<16} n={m['n']:>4}  PnL={m['net_pct']:+7.2f}%  WR={m['win_rate']:4.1f}%  "
          f"exp={m['expectancy']:+.3f}%  PF={pf_s:>5}  maxDD={m['max_dd']:.2f}%")


# ───────────────────── Main ──────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbol", type=str, default=None, help="Un solo par (ej. SOLUSDC)")
    ap.add_argument("--cooldown-candles", type=int, default=24, help="Velas de cooldown post-SL (24×5m=2h)")
    args = ap.parse_args()

    bcfg = config.binance
    if not bcfg.has_credentials:
        print("ERROR: faltan BINANCE_API_KEY / BINANCE_API_SECRET en el entorno/.env")
        sys.exit(1)

    client = Client(bcfg.api_key, bcfg.api_secret, testnet=bcfg.testnet)
    scfg = config.strategy
    costs = config.costs
    risk = config.get_risk_params()

    if args.symbol:
        symbols = [args.symbol]
    else:
        symbols = [p.symbol for p in config.trading_pairs if p.enabled]

    print(f"Backtest {args.days}d · timeframe {scfg.timeframe} · risk_level {config.risk_level} "
          f"(SL {risk['stop_loss_pct']}% / TP {risk['take_profit_pct']}% / umbral {risk['signal_threshold']})")
    print(f"Fees {costs.effective_fee_rate*100:.3f}%/lado · slippage {costs.slippage_base_pct:.3f}%/lado · "
          f"anti-chop ADX≥{scfg.adx_min} · cost-gate ATR≥{scfg.cost_gate_atr_mult}×coste\n")

    agg_base, agg_filt = [], []
    for sym in symbols:
        df = fetch_klines(client, sym, scfg.timeframe, args.days)
        if df is None or len(df) < 100:
            print(f"{sym}: datos insuficientes"); continue
        base = simulate(df, scfg, costs, risk, use_filters=False, post_sl_cooldown_candles=0)
        filt = simulate(df, scfg, costs, risk, use_filters=True, post_sl_cooldown_candles=args.cooldown_candles)
        print(f"{sym}  ({len(df)} velas)")
        show("A · sin filtros", metrics(base))
        show("B · con filtros", metrics(filt))
        print()
        agg_base += base
        agg_filt += filt

    print("=" * 70)
    print("TOTAL (todos los pares)")
    show("A · sin filtros", metrics(agg_base))
    show("B · con filtros", metrics(agg_filt))
    print("=" * 70)
    mb, mf = metrics(agg_base), metrics(agg_filt)
    if mb.get("n") and mf.get("n"):
        print(f"\nEfecto de los filtros: PnL {mb['net_pct']:+.2f}% → {mf['net_pct']:+.2f}%  | "
              f"WR {mb['win_rate']:.1f}% → {mf['win_rate']:.1f}%  | "
              f"trades {mb['n']} → {mf['n']} ({100*(mf['n']-mb['n'])/mb['n']:+.0f}%)")
        print("\nObjetivo para ir a real: B con expectancy > 0, profit factor > 1.3, y menos trades que A.")


if __name__ == "__main__":
    main()
