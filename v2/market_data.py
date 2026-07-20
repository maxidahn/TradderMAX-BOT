"""
Celerity v2 — Fuente de datos de mercado (mirror público de Binance)
=====================================================================
Binance.com bloquea las IPs de muchos servidores/regiones (ej. EE.UU., donde
corre Railway por defecto) → "Service unavailable from a restricted location".

Solución: bajar los precios del mirror PÚBLICO de datos de Binance
(`data-api.binance.vision`), que NO está bloqueado por ubicación y no necesita
claves. Son datos de mercado spot; para un bot de tendencia en BTC/ETH el precio
spot ≈ perpetuo (difieren centavos), así que sirve perfecto — sobre todo en paper.

Solo se usa para DATOS (velas, precio, filtros). Las órdenes reales en live
siguen yendo por el cliente autenticado de futures en executor_v2.
"""
import logging
import requests
import pandas as pd

logger = logging.getLogger("celerity.v2.data")

BASE = "https://data-api.binance.vision/api/v3"
_SESSION = requests.Session()
_INFO_CACHE: dict = {}

# Defaults sensatos si el exchangeInfo no está disponible (sizing en paper)
_DEFAULT_INFO = {"step": 0.00001, "min_qty": 0.00001, "min_notional": 5.0, "qty_prec": 5}


def get_klines(symbol: str, interval: str = "4h", limit: int = 400) -> pd.DataFrame:
    r = _SESSION.get(f"{BASE}/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=15)
    r.raise_for_status()
    k = r.json()
    df = pd.DataFrame(k, columns=["t", "open", "high", "low", "close", "v",
                                  "ct", "qv", "n", "tb", "tq", "ig"])
    for c in ["open", "high", "low", "close", "v"]:
        df[c] = df[c].astype(float)
    return df[["t", "open", "high", "low", "close", "v"]]


def get_price(symbol: str) -> float:
    r = _SESSION.get(f"{BASE}/ticker/price", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def get_symbol_info(symbol: str) -> dict:
    if symbol in _INFO_CACHE:
        return _INFO_CACHE[symbol]
    try:
        r = _SESSION.get(f"{BASE}/exchangeInfo", params={"symbol": symbol}, timeout=15)
        r.raise_for_status()
        s = r.json()["symbols"][0]
        f = {x["filterType"]: x for x in s["filters"]}
        step = float(f.get("LOT_SIZE", {}).get("stepSize", _DEFAULT_INFO["step"]))
        # precisión decimal a partir del stepSize (ej. 0.001 -> 3)
        prec = max(0, len(f"{step:.10f}".rstrip("0").split(".")[1])) if step < 1 else 0
        info = {
            "step": step,
            "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", _DEFAULT_INFO["min_qty"])),
            "min_notional": float(f.get("NOTIONAL", {}).get("minNotional",
                              f.get("MIN_NOTIONAL", {}).get("minNotional", _DEFAULT_INFO["min_notional"]))),
            "qty_prec": prec,
        }
        _INFO_CACHE[symbol] = info
        return info
    except Exception as e:
        logger.warning("get_symbol_info %s falló (%s) → uso defaults", symbol, e)
        return dict(_DEFAULT_INFO)


def reachable() -> bool:
    try:
        get_price("BTCUSDT")
        return True
    except Exception as e:
        logger.warning("mirror de datos no alcanzable: %s", e)
        return False
