"""
Celerity Trader Bot — Perpetuals Data Provider
================================================
Acceso a datos específicos de perpetuos que el spot bot no usa:
  - Funding rate (qué pagan los longs a los shorts cada 8h)
  - Open interest (interés abierto total — confirma momentum)
  - Long/Short ratio (sesgo de posicionamiento minorista — señal contraria)

Estos datos son alfa real en perpetuos:
  - Funding extremo positivo (longs pagan mucho) → tops de mercado
  - Funding extremo negativo (shorts pagan mucho) → bottoms
  - L/S ratio extremo (>3 o <0.4) → contra el sesgo del retail

Todo cacheado (Binance limita ~1200 req/min).
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("celerity.perpdata")


@dataclass
class PerpetualMetrics:
    """Snapshot de métricas de perpetuo para un símbolo."""
    symbol: str
    funding_rate: float            # % (8h rate, e.g. 0.01 = 0.01% per 8h)
    next_funding_time: int         # ms timestamp
    open_interest: float           # In base asset units (e.g. SOL contracts)
    open_interest_usdt: float      # Approximate USDT value
    long_short_ratio: float        # >1 = más longs, <1 = más shorts
    top_trader_ls_ratio: float     # L/S ratio of top accounts (more sophisticated)
    timestamp: int                 # When this snapshot was taken (s)


class PerpetualsData:
    """Wrapper alrededor de python-binance para métricas de perpetuos USD-M."""

    CACHE_TTL = 30  # 30s — funding/OI no cambian rápido

    def __init__(self, client=None):
        self.client = client      # python-binance Client instance (puede ser None inicialmente)
        self._cache: Dict[str, dict] = {}

    def set_client(self, client):
        """Inyectado por FuturesTrader una vez conectado."""
        self.client = client

    def get_metrics(self, symbol: str) -> Optional[PerpetualMetrics]:
        """
        Snapshot completo de métricas para un símbolo. Cacheado 30s.
        Devuelve None si no hay cliente / API falla — el agente debe tolerar.
        """
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and now - cached["t"] < self.CACHE_TTL:
            return cached["metrics"]

        if not self.client:
            return None

        try:
            # Funding rate
            funding_rate = 0.0
            next_funding_time = 0
            try:
                mark = self.client.futures_mark_price(symbol=symbol)
                funding_rate = float(mark.get("lastFundingRate", 0)) * 100  # → %
                next_funding_time = int(mark.get("nextFundingTime", 0))
            except Exception as e:
                logger.debug(f"funding_rate {symbol}: {e}")

            # Open interest
            oi_base = 0.0
            oi_usdt = 0.0
            try:
                oi = self.client.futures_open_interest(symbol=symbol)
                oi_base = float(oi.get("openInterest", 0))
                # Convert to USDT using last price
                ticker = self.client.futures_symbol_ticker(symbol=symbol)
                last_price = float(ticker.get("price", 0))
                oi_usdt = oi_base * last_price
            except Exception as e:
                logger.debug(f"open_interest {symbol}: {e}")

            # Long/Short ratio (global account count)
            ls_ratio = 1.0
            try:
                # python-binance method name: futures_global_longshort_account_ratio
                if hasattr(self.client, "futures_global_longshort_account_ratio"):
                    data = self.client.futures_global_longshort_account_ratio(
                        symbol=symbol, period="5m", limit=1
                    )
                    if data:
                        ls_ratio = float(data[0].get("longShortRatio", 1.0))
            except Exception as e:
                logger.debug(f"ls_ratio {symbol}: {e}")

            # Top trader L/S ratio (más sofisticado)
            top_ls = 1.0
            try:
                if hasattr(self.client, "futures_top_longshort_account_ratio"):
                    data = self.client.futures_top_longshort_account_ratio(
                        symbol=symbol, period="5m", limit=1
                    )
                    if data:
                        top_ls = float(data[0].get("longShortRatio", 1.0))
            except Exception as e:
                logger.debug(f"top_ls {symbol}: {e}")

            metrics = PerpetualMetrics(
                symbol=symbol,
                funding_rate=funding_rate,
                next_funding_time=next_funding_time,
                open_interest=oi_base,
                open_interest_usdt=oi_usdt,
                long_short_ratio=ls_ratio,
                top_trader_ls_ratio=top_ls,
                timestamp=int(now),
            )
            self._cache[symbol] = {"t": now, "metrics": metrics}
            return metrics
        except Exception as e:
            logger.warning(f"PerpetualsData.get_metrics {symbol} failed: {e}")
            return None

    def funding_extreme_signal(self, symbol: str, threshold_pct: float = 0.03) -> Optional[str]:
        """
        Devuelve 'SHORT' si funding es muy positivo (longs sobrepagando → top),
        'LONG' si funding es muy negativo (shorts sobrepagando → bottom),
        None si está dentro de rangos normales.
        """
        m = self.get_metrics(symbol)
        if not m:
            return None
        if m.funding_rate >= threshold_pct:
            return "SHORT"
        if m.funding_rate <= -threshold_pct:
            return "LONG"
        return None

    def crowded_long_signal(self, symbol: str, threshold: float = 3.0) -> bool:
        """L/S ratio > threshold → minoristas muy long → señal contraria."""
        m = self.get_metrics(symbol)
        if not m:
            return False
        return m.long_short_ratio >= threshold

    def crowded_short_signal(self, symbol: str, threshold: float = 0.4) -> bool:
        """L/S ratio < threshold → minoristas muy short → señal contraria."""
        m = self.get_metrics(symbol)
        if not m:
            return False
        return m.long_short_ratio <= threshold
