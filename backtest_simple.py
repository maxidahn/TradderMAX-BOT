"""
backtest_simple.py — Backtest honesto de la estrategia "Celerity v2" (trend / cash)
====================================================================================
Compara, sobre datos REALES de Binance y con fees REALES:

  1. BUY & HOLD            (comprar y aguantar)
  2. SCALP 5m (proxy)      cruce EMA9/21 long-only, parecido al bot actual
  3. CELERITY v2 (TREND)   la estrategia simple propuesta (long/cash en daily)

Por qué este diseño (ver REPORTE_CELERITY_*.md):
  - El bot actual hace ~26 trades/semana en 5m → las fees (0.1% x 2) se comen el edge.
  - Su payoff es 0.86x cuando necesita 2.1x. El trend-following invierte eso:
    win rate bajo (~40%) pero ganadores grandes → payoff > 2x.
  - Sólo estás largo cuando la tendencia de fondo es alcista; si no, CASH (USDC).
    Eso solo habría evitado la mayor parte de los 26 stop-losses (-$23.80).

REGLAS de CELERITY v2 (spot, long/cash, decisión 1 vez por día con velas diarias):
  - Universo: pocos pares de máxima liquidez (BTC, ETH).
  - Filtro de régimen: sólo operar si Close > EMA200 (mercado alcista de fondo).
  - Entrada (LONG): EMA_fast(20) > EMA_slow(50) Y Close rompe el máximo de Donchian(20).
  - Salida: Close < EMA_slow(50)  O  trailing por Chandelier (máximo - 3*ATR14).
  - Riesgo: se arriesga RISK_PCT (1%) del equity por trade; el tamaño sale del stop ATR.
  - Sin take-profit fijo: se deja correr al ganador (eso es lo único que ganaba en tu historial).

USO:
    pip install requests pandas numpy
    python backtest_simple.py
(Corré esto en TU máquina: desde el servidor del bot Binance es alcanzable.)
"""

import time
import math
import requests
import numpy as np
import pandas as pd

# ─── Parámetros ────────────────────────────────────────────────────────────────
PAIRS        = ["BTCUSDT", "ETHUSDT"]   # máxima liquidez. Podés sumar "SOLUSDT".
INTERVAL     = "1d"                      # velas DIARIAS (clave: mata el over-trading)
LOOKBACK     = 700                       # ~2 años de historia diaria
FEE          = 0.001                     # 0.1% por lado (taker spot Binance)
SLIPPAGE     = 0.0005                    # 0.05% estimado por lado
RISK_PCT     = 0.01                      # 1% del equity arriesgado por trade
START_EQUITY = 500.0

EMA_FAST, EMA_SLOW, EMA_TREND = 20, 50, 200
DONCHIAN, ATR_LEN, CHAND_MULT = 20, 14, 3.0

BINANCE = "https://data-api.binance.vision/api/v3/klines"


def get_klines(symbol, interval=INTERVAL, limit=LOOKBACK):
    out, end = [], None
    while len(out) < limit:
        params = {"symbol": symbol, "interval": interval, "limit": min(1000, limit - len(out))}
        if end:
            params["endTime"] = end
        r = requests.get(BINANCE, params=params, timeout=20)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        out = k + out
        end = k[0][0] - 1
        time.sleep(0.2)
    df = pd.DataFrame(out, columns=["t","o","h","l","c","v","ct","qv","n","tb","tq","ig"])
    for col in ["o","h","l","c","v"]:
        df[col] = df[col].astype(float)
    df["date"] = pd.to_datetime(df["t"], unit="ms")
    return df.drop_duplicates("t").reset_index(drop=True)


def atr(df, n=ATR_LEN):
    h, l, c = df["h"], df["l"], df["c"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def metrics(trades, equity_curve):
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins); gross_l = abs(sum(losses))
    eq = np.array(equity_curve); peak = np.maximum.accumulate(eq)
    maxdd = ((eq - peak) / peak).min() * 100 if len(eq) else 0
    return {
        "trades": len(trades),
        "win_rate": round(len(wins)/len(trades)*100, 1),
        "expectancy_$": round(sum(pnls)/len(trades), 3),
        "payoff": round((np.mean(wins) if wins else 0)/(abs(np.mean(losses)) if losses else 1), 2),
        "profit_factor": round(gross_w/gross_l, 2) if gross_l else float("inf"),
        "net_pnl_$": round(sum(pnls), 2),
        "return_pct": round((equity_curve[-1]/START_EQUITY - 1)*100, 1) if equity_curve else 0,
        "max_drawdown_pct": round(maxdd, 1),
    }


def backtest_trend(df):
    """CELERITY v2: long/cash en diario, riesgo por ATR, deja correr ganadores."""
    df = df.copy()
    df["ema_f"] = df["c"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["c"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["ema_t"] = df["c"].ewm(span=EMA_TREND, adjust=False).mean()
    df["donch"] = df["h"].rolling(DONCHIAN).max().shift(1)
    df["atr"] = atr(df)

    equity = START_EQUITY
    pos = None
    trades, curve = [], []
    for i in range(EMA_TREND, len(df)):
        row = df.iloc[i]
        price = row["c"]
        if pos is None:
            regime_ok = price > row["ema_t"]
            trend_ok  = row["ema_f"] > row["ema_s"]
            breakout  = price > row["donch"] if not math.isnan(row["donch"]) else False
            if regime_ok and trend_ok and breakout and row["atr"] > 0:
                stop = price - CHAND_MULT * row["atr"]
                risk_per_unit = price - stop
                qty = (equity * RISK_PCT) / risk_per_unit
                cost = qty * price
                if cost > equity:                      # sin apalancamiento
                    qty = equity / price
                entry = price * (1 + SLIPPAGE)
                equity -= qty * entry * FEE
                pos = {"entry": entry, "qty": qty, "peak": price, "stop": stop}
        else:
            pos["peak"] = max(pos["peak"], row["h"])
            chand = pos["peak"] - CHAND_MULT * row["atr"]
            stop = max(pos["stop"], chand)
            exit_trend = price < row["ema_s"]
            hit_stop   = row["l"] <= stop
            if exit_trend or hit_stop:
                ex = (stop if hit_stop else price) * (1 - SLIPPAGE)
                gross = (ex - pos["entry"]) * pos["qty"]
                fee = pos["qty"] * ex * FEE
                pnl = gross - fee
                equity += pnl
                trades.append({"pnl": pnl})
                pos = None
        curve.append(equity if pos is None else equity + pos["qty"]*(price-pos["entry"]))
    return metrics(trades, curve)


def backtest_scalp(df):
    """Proxy del bot actual: cruce EMA9/21 long-only en la MISMA serie, fee taker x2."""
    df = df.copy()
    df["ef"] = df["c"].ewm(span=9, adjust=False).mean()
    df["es"] = df["c"].ewm(span=21, adjust=False).mean()
    equity = START_EQUITY; pos = None; trades, curve = [], []
    for i in range(21, len(df)):
        row = df.iloc[i]; price = row["c"]
        bull = row["ef"] > row["es"]
        if pos is None and bull:
            entry = price*(1+SLIPPAGE); qty = equity/price
            equity -= qty*entry*FEE; pos = {"entry": entry, "qty": qty}
        elif pos and not bull:
            ex = price*(1-SLIPPAGE); pnl = (ex-pos["entry"])*pos["qty"] - pos["qty"]*ex*FEE
            equity += pnl; trades.append({"pnl": pnl}); pos = None
        curve.append(equity if pos is None else equity + pos["qty"]*(price-pos["entry"]))
    return metrics(trades, curve)


def backtest_hold(df):
    df = df.iloc[EMA_TREND:].copy()
    p0, p1 = df["c"].iloc[0], df["c"].iloc[-1]
    qty = START_EQUITY/p0
    end = qty*p1*(1-FEE)
    curve = list(qty*df["c"].values)
    return {"trades": 1, "return_pct": round((end/START_EQUITY-1)*100,1),
            "net_pnl_$": round(end-START_EQUITY,2),
            "max_drawdown_pct": round(((np.array(curve)-np.maximum.accumulate(curve))/np.maximum.accumulate(curve)).min()*100,1)}


if __name__ == "__main__":
    print(f"\n{'='*78}\nBACKTEST CELERITY v2 — {INTERVAL} — fee {FEE*100:.2f}%/lado + slippage {SLIPPAGE*100:.2f}%\n{'='*78}")
    for sym in PAIRS:
        try:
            df = get_klines(sym)
        except Exception as e:
            print(f"\n{sym}: no pude bajar datos ({e})"); continue
        print(f"\n### {sym}  ({df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()}, {len(df)} velas)")
        print(f"  {'Buy & Hold':<18}", backtest_hold(df))
        print(f"  {'Scalp EMA (bot)':<18}", backtest_scalp(df))
        print(f"  {'CELERITY v2 TREND':<18}", backtest_trend(df))
    print(f"\nNota: el objetivo de v2 NO es ganar siempre — es payoff>2x con pocos trades,")
    print(f"perder poco en mercado bajista (cash) y dejar correr al ganador en alcista.\n")
