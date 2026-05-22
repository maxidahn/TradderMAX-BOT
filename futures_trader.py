"""
Celerity Trader Bot — Binance Futures Trader (USDT-M Perpetuals)
==================================================================
Wrapper alrededor de python-binance que añade:
  - Apertura de LONG y SHORT (hedge / one-way mode auto-detect)
  - Gestión de apalancamiento por símbolo
  - Modo paper (simulado, no toca Binance) ↔ live
  - Soporte de SL/TP dinámicos por agente
  - PnL realista descontando fees y funding pagado durante el hold

Importante:
  - Spot bot vive en otro mundo: `trader.py`. Este archivo NO lo toca.
  - Mantiene un balance virtual cuando paper_trade=True.
  - Las posiciones se persisten en data/futures_positions.json para sobrevivir
    reinicios (los agentes son stateless entre ciclos).
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("celerity.futures")

DATA_DIR = os.getenv("DATA_DIR", "data")
POSITIONS_FILE = os.path.join(DATA_DIR, "futures_positions.json")
HISTORY_FILE   = os.path.join(DATA_DIR, "futures_trades.json")
EQUITY_FILE    = os.path.join(DATA_DIR, "futures_equity.json")


# ─────────────────────────────────────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FuturesPosition:
    """An open perpetual position."""
    symbol: str
    side: str             # 'LONG' or 'SHORT'
    entry_price: float
    quantity: float       # In base asset (e.g. SOL)
    notional: float       # USDT exposure at entry
    leverage: int
    entry_time: str       # ISO timestamp
    agent: str            # Name of the agent that opened it
    sl_price: float = 0.0
    tp_price: float = 0.0
    peak_price: float = 0.0       # For trailing stop (highest seen for LONG, lowest for SHORT)
    funding_paid: float = 0.0     # Accumulated funding payments
    order_id: str = ""
    paper: bool = True
    metadata: dict = field(default_factory=dict)   # Anything the agent wants to remember


@dataclass
class FuturesTradeRecord:
    """Closed trade outcome — fuente de verdad del aprendizaje."""
    symbol: str
    side: str
    agent: str
    entry_price: float
    exit_price: float
    quantity: float
    notional: float
    leverage: int
    entry_time: str
    exit_time: str
    pnl_usdt: float           # Net (after fees & funding)
    pnl_pct: float            # On notional
    pnl_gross: float
    fees: float
    funding_paid: float
    reason: str               # 'TP', 'SL', 'TRAILING', 'TIMEOUT', 'AGENT_SIGNAL'
    hold_minutes: float
    paper: bool
    order_id: str = ""
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
#  FuturesTrader
# ─────────────────────────────────────────────────────────────────────────────

# Binance Futures fee — taker 0.04%, maker 0.02%. We assume taker (market orders).
FUTURES_FEE_TAKER = 0.0004
FUTURES_FEE_MAKER = 0.0002


class FuturesTrader:
    """Wrapper alrededor de python-binance para Futures USD-M con paper/live mode."""

    def __init__(self, binance_config, futures_config):
        self.binance_config = binance_config
        self.fc = futures_config
        self.client = None
        self.connected = False
        self._lock = threading.Lock()

        # ── Symbol metadata cache (filters: stepSize, minQty, minNotional) ────
        self._symbol_info: Dict[str, dict] = {}

        # ── Paper-mode virtual state ─────────────────────────────────────────
        self.paper_equity: float = futures_config.paper_starting_equity
        self.realized_pnl_today: float = 0.0
        self._today_iso: str = datetime.now(timezone.utc).date().isoformat()

        # ── Position / history persistence ───────────────────────────────────
        os.makedirs(DATA_DIR, exist_ok=True)
        self.positions: Dict[str, FuturesPosition] = self._load_positions()
        self.history: List[FuturesTradeRecord] = self._load_history()
        self._load_equity()

    # ─── Connection ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Establece conexión con Binance Futures (igual en paper y live; necesitamos klines)."""
        if not self.binance_config.has_credentials:
            logger.error(f"FuturesTrader: {self.binance_config.credentials_status}")
            # In paper mode we can still proceed using public endpoints — try anyway
            try:
                from binance.client import Client
                self.client = Client()  # No creds → public-only
                logger.warning("FuturesTrader: connected with PUBLIC-only client (paper-trade can still run)")
                self.connected = True
                return True
            except Exception as e:
                logger.error(f"FuturesTrader: public-only client failed: {e}")
                return False

        try:
            from binance.client import Client
            self.client = Client(
                self.binance_config.api_key,
                self.binance_config.api_secret,
                testnet=self.binance_config.testnet,
            )
            # Test futures endpoint
            self.client.futures_ping()
            self.connected = True
            mode = "TESTNET" if self.binance_config.testnet else "LIVE"
            paper = "PAPER" if self.fc.paper_trade else "REAL ORDERS"
            logger.info(f"FuturesTrader: connected ({mode}, {paper})")
            return True
        except Exception as e:
            logger.error(f"FuturesTrader: connect failed — {e}")
            return False

    # ─── Market data ─────────────────────────────────────────────────────────

    def get_candles(self, symbol: str, interval: str = "5m", limit: int = 200) -> Optional[pd.DataFrame]:
        """Fetch klines from Binance Futures. Lower spread than spot for the same pair."""
        if not self.client:
            return None
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(klines, columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            return df[["timestamp", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            logger.error(f"FuturesTrader: get_candles {symbol} failed: {e}")
            return None

    def get_mark_price(self, symbol: str) -> Optional[float]:
        """Mark price (oracle-style, used for liquidations)."""
        if not self.client:
            return None
        try:
            data = self.client.futures_mark_price(symbol=symbol)
            return float(data["markPrice"])
        except Exception as e:
            logger.debug(f"get_mark_price {symbol}: {e}")
            return None

    def get_ticker_price(self, symbol: str) -> Optional[float]:
        """Last trade price."""
        if not self.client:
            return None
        try:
            data = self.client.futures_symbol_ticker(symbol=symbol)
            return float(data["price"])
        except Exception as e:
            logger.debug(f"get_ticker_price {symbol}: {e}")
            return None

    def _get_symbol_info(self, symbol: str) -> dict:
        """Cache LOT_SIZE / MIN_NOTIONAL filters per symbol."""
        if symbol in self._symbol_info:
            return self._symbol_info[symbol]
        if not self.client:
            return {}
        try:
            info = self.client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    filters = {f["filterType"]: f for f in s.get("filters", [])}
                    parsed = {
                        "step_size":  float(filters.get("LOT_SIZE", {}).get("stepSize", 0.001)),
                        "min_qty":    float(filters.get("LOT_SIZE", {}).get("minQty", 0.001)),
                        "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
                        "price_precision": int(s.get("pricePrecision", 2)),
                        "quantity_precision": int(s.get("quantityPrecision", 3)),
                    }
                    self._symbol_info[symbol] = parsed
                    return parsed
        except Exception as e:
            logger.debug(f"futures_exchange_info failed: {e}")
        # Sensible defaults
        return {"step_size": 0.001, "min_qty": 0.001, "min_notional": 5.0,
                "price_precision": 2, "quantity_precision": 3}

    def _round_qty(self, symbol: str, qty: float) -> float:
        info = self._get_symbol_info(symbol)
        step = info.get("step_size", 0.001)
        prec = info.get("quantity_precision", 3)
        return round(qty - (qty % step), prec)

    # ─── Leverage management ─────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol. Capped by FuturesConfig.max_leverage."""
        leverage = max(1, min(self.fc.max_leverage, int(leverage)))
        if self.fc.paper_trade:
            logger.debug(f"FuturesTrader[paper]: leverage {symbol} → {leverage}x (simulated)")
            return True
        if not self.client:
            return False
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info(f"FuturesTrader: leverage {symbol} → {leverage}x")
            return True
        except Exception as e:
            logger.warning(f"set_leverage {symbol} failed: {e}")
            return False

    # ─── Order placement ─────────────────────────────────────────────────────

    def open_position(
        self,
        symbol: str,
        side: str,                 # 'LONG' or 'SHORT'
        notional_usdt: float,
        leverage: int,
        agent: str,
        sl_pct: float,
        tp_pct: float,
        metadata: Optional[dict] = None,
    ) -> Optional[FuturesPosition]:
        """
        Open a new perpetual position (LONG or SHORT).
        Returns FuturesPosition on success.
        """
        with self._lock:
            if symbol in self.positions:
                logger.info(f"open_position {symbol}: already in position, skipping")
                return None

            if len(self.positions) >= self.fc.max_open_positions:
                logger.info(f"open_position {symbol}: max positions ({self.fc.max_open_positions}) reached")
                return None

            side = side.upper()
            if side not in ("LONG", "SHORT"):
                logger.error(f"open_position: invalid side {side}")
                return None

            # Reset daily PnL on new day
            today = datetime.now(timezone.utc).date().isoformat()
            if today != self._today_iso:
                self.realized_pnl_today = 0.0
                self._today_iso = today

            # Kill switch — daily loss limit
            limit = -self.fc.max_daily_loss_pct / 100.0 * self.paper_equity
            if self.realized_pnl_today <= limit:
                logger.warning(
                    f"KILL SWITCH ACTIVE: daily PnL ${self.realized_pnl_today:.2f} "
                    f"<= limit ${limit:.2f} — blocking new entries"
                )
                return None

            # Get entry price
            price = self.get_ticker_price(symbol)
            if not price or price <= 0:
                logger.error(f"open_position {symbol}: no price available")
                return None

            leverage = max(1, min(self.fc.max_leverage, int(leverage)))
            self.set_leverage(symbol, leverage)

            # Calculate quantity from notional
            qty = notional_usdt / price
            qty = self._round_qty(symbol, qty)
            if qty <= 0:
                logger.error(f"open_position {symbol}: quantity rounds to 0")
                return None

            # Validate min notional
            info = self._get_symbol_info(symbol)
            actual_notional = qty * price
            if actual_notional < info.get("min_notional", 5.0):
                logger.error(
                    f"open_position {symbol}: notional ${actual_notional:.2f} < min "
                    f"${info.get('min_notional', 5.0):.2f}"
                )
                return None

            # Calculate SL/TP prices
            if side == "LONG":
                sl_price = price * (1 - sl_pct / 100.0)
                tp_price = price * (1 + tp_pct / 100.0)
            else:  # SHORT
                sl_price = price * (1 + sl_pct / 100.0)
                tp_price = price * (1 - tp_pct / 100.0)

            order_id = ""
            if self.fc.paper_trade:
                # Simulate fee deduction on entry
                entry_fee = actual_notional * FUTURES_FEE_TAKER
                self.paper_equity -= entry_fee
                order_id = f"PAPER-{int(time.time()*1000)}"
                logger.info(
                    f"📝 PAPER {side} {symbol}: qty={qty} @ ${price:.4f} "
                    f"notional=${actual_notional:.2f} lev={leverage}x "
                    f"SL=${sl_price:.4f} TP=${tp_price:.4f} (fee ${entry_fee:.4f})"
                )
            else:
                # Real Binance Futures order
                try:
                    binance_side = "BUY" if side == "LONG" else "SELL"
                    order = self.client.futures_create_order(
                        symbol=symbol,
                        side=binance_side,
                        type="MARKET",
                        quantity=qty,
                    )
                    order_id = str(order.get("orderId", ""))
                    # Note: actual fill price may differ — refresh
                    refreshed = self.get_ticker_price(symbol)
                    if refreshed:
                        price = refreshed
                        if side == "LONG":
                            sl_price = price * (1 - sl_pct / 100.0)
                            tp_price = price * (1 + tp_pct / 100.0)
                        else:
                            sl_price = price * (1 + sl_pct / 100.0)
                            tp_price = price * (1 - tp_pct / 100.0)
                    logger.info(
                        f"🔴 LIVE {side} {symbol}: qty={qty} @ ~${price:.4f} "
                        f"order_id={order_id}"
                    )
                except Exception as e:
                    logger.error(f"open_position {symbol} LIVE FAILED: {e}")
                    return None

            position = FuturesPosition(
                symbol=symbol,
                side=side,
                entry_price=price,
                quantity=qty,
                notional=actual_notional,
                leverage=leverage,
                entry_time=datetime.now(timezone.utc).isoformat(),
                agent=agent,
                sl_price=sl_price,
                tp_price=tp_price,
                peak_price=price,
                order_id=order_id,
                paper=self.fc.paper_trade,
                metadata=metadata or {},
            )
            self.positions[symbol] = position
            self._save_positions()
            return position

    def close_position(self, symbol: str, reason: str = "AGENT_SIGNAL") -> Optional[FuturesTradeRecord]:
        """Close an existing position at market price."""
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None

            exit_price = self.get_ticker_price(symbol)
            if not exit_price or exit_price <= 0:
                logger.error(f"close_position {symbol}: no exit price available")
                return None

            order_id = ""
            if not self.fc.paper_trade:
                try:
                    # To close LONG → SELL; to close SHORT → BUY
                    close_side = "SELL" if pos.side == "LONG" else "BUY"
                    order = self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type="MARKET",
                        quantity=pos.quantity,
                        reduceOnly=True,
                    )
                    order_id = str(order.get("orderId", ""))
                    refreshed = self.get_ticker_price(symbol)
                    if refreshed:
                        exit_price = refreshed
                except Exception as e:
                    logger.error(f"close_position {symbol} LIVE FAILED: {e}")
                    return None

            # Compute PnL (perp PnL = (exit - entry) * qty for LONG, inverted for SHORT)
            if pos.side == "LONG":
                pnl_gross = (exit_price - pos.entry_price) * pos.quantity
            else:
                pnl_gross = (pos.entry_price - exit_price) * pos.quantity

            entry_fee = pos.notional * FUTURES_FEE_TAKER
            exit_notional = exit_price * pos.quantity
            exit_fee  = exit_notional * FUTURES_FEE_TAKER
            total_fees = entry_fee + exit_fee
            pnl_net = pnl_gross - total_fees - pos.funding_paid
            pnl_pct = pnl_net / pos.notional * 100.0 if pos.notional > 0 else 0.0

            entry_dt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
            hold_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0

            if self.fc.paper_trade:
                self.paper_equity += pnl_net   # PnL realized
                self.realized_pnl_today += pnl_net

            record = FuturesTradeRecord(
                symbol=symbol,
                side=pos.side,
                agent=pos.agent,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                quantity=pos.quantity,
                notional=pos.notional,
                leverage=pos.leverage,
                entry_time=pos.entry_time,
                exit_time=datetime.now(timezone.utc).isoformat(),
                pnl_usdt=round(pnl_net, 4),
                pnl_pct=round(pnl_pct, 3),
                pnl_gross=round(pnl_gross, 4),
                fees=round(total_fees, 4),
                funding_paid=round(pos.funding_paid, 4),
                reason=reason,
                hold_minutes=round(hold_min, 1),
                paper=pos.paper,
                order_id=order_id,
                metadata=pos.metadata,
            )

            self.history.append(record)
            del self.positions[symbol]
            self._save_positions()
            self._save_history()
            self._save_equity()

            emoji = "💰" if pnl_net >= 0 else "📉"
            mode = "📝" if pos.paper else "🔴"
            logger.info(
                f"{mode} {emoji} CLOSE {pos.side} {symbol} ({reason}) @ ${exit_price:.4f} | "
                f"PnL: ${pnl_net:+.4f} ({pnl_pct:+.2f}%) | hold {hold_min:.0f}min | "
                f"by {pos.agent}"
            )
            return record

    # ─── Position monitoring (SL/TP/trailing/timeout) ────────────────────────

    def check_position_exits(self, max_hold_min: int = 240, trailing_after_pct: float = 1.0,
                              trailing_distance_pct: float = 0.8) -> List[FuturesTradeRecord]:
        """
        Check all open positions for SL/TP/trailing/timeout exits.
        Returns list of closed trades.
        """
        closed = []
        for symbol in list(self.positions.keys()):
            pos = self.positions.get(symbol)
            if not pos:
                continue

            price = self.get_ticker_price(symbol)
            if not price:
                continue

            # Update peak for trailing stop
            if pos.side == "LONG":
                if price > pos.peak_price:
                    pos.peak_price = price
                gain_pct = (price - pos.entry_price) / pos.entry_price * 100.0
            else:  # SHORT
                if pos.peak_price == 0 or price < pos.peak_price:
                    pos.peak_price = price
                gain_pct = (pos.entry_price - price) / pos.entry_price * 100.0

            # SL check
            if pos.side == "LONG" and price <= pos.sl_price:
                rec = self.close_position(symbol, reason="SL")
                if rec:
                    closed.append(rec)
                continue
            if pos.side == "SHORT" and price >= pos.sl_price:
                rec = self.close_position(symbol, reason="SL")
                if rec:
                    closed.append(rec)
                continue

            # TP check
            if pos.side == "LONG" and price >= pos.tp_price:
                rec = self.close_position(symbol, reason="TP")
                if rec:
                    closed.append(rec)
                continue
            if pos.side == "SHORT" and price <= pos.tp_price:
                rec = self.close_position(symbol, reason="TP")
                if rec:
                    closed.append(rec)
                continue

            # Trailing stop — only once gain ≥ trailing_after_pct
            if gain_pct >= trailing_after_pct:
                if pos.side == "LONG":
                    drawdown_pct = (pos.peak_price - price) / pos.peak_price * 100.0
                else:
                    drawdown_pct = (price - pos.peak_price) / pos.peak_price * 100.0
                if drawdown_pct >= trailing_distance_pct:
                    rec = self.close_position(symbol, reason="TRAILING")
                    if rec:
                        closed.append(rec)
                    continue

            # Timeout
            entry_dt = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
            hold_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0
            if hold_min >= max_hold_min:
                rec = self.close_position(symbol, reason="TIMEOUT")
                if rec:
                    closed.append(rec)

        return closed

    # ─── Funding (subtracted from PnL during hold) ───────────────────────────

    def apply_funding(self, symbol: str, funding_rate: float):
        """
        Aplica un pago de funding a una posición abierta (positivo = pagamos si LONG,
        cobramos si SHORT). Llamado típicamente cada 8h cuando Binance liquida funding.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return
            # Funding rate aplica sobre notional actual al mark price (aprox = entry * qty)
            payment = pos.notional * funding_rate
            # LONG paga si funding > 0, cobra si funding < 0
            if pos.side == "LONG":
                pos.funding_paid += payment
            else:
                pos.funding_paid -= payment
            self._save_positions()
            logger.debug(f"funding {symbol} {pos.side}: rate={funding_rate:.4f}% → paid={pos.funding_paid:+.4f}")

    # ─── Account info ────────────────────────────────────────────────────────

    def get_futures_balance_usdt(self) -> float:
        """Available USDT balance in Futures wallet (live) or paper equity."""
        if self.fc.paper_trade:
            return self.paper_equity
        if not self.client:
            return 0.0
        try:
            balances = self.client.futures_account_balance()
            for b in balances:
                if b["asset"] == "USDT":
                    return float(b["availableBalance"])
        except Exception as e:
            logger.debug(f"get_futures_balance_usdt: {e}")
        return 0.0

    def get_status(self) -> dict:
        """Status dict for the dashboard."""
        wins = sum(1 for t in self.history if t.pnl_usdt > 0)
        total = len(self.history)
        win_rate = round(wins / total * 100.0, 1) if total > 0 else 0.0
        total_pnl = round(sum(t.pnl_usdt for t in self.history), 4)
        equity = self.paper_equity if self.fc.paper_trade else self.get_futures_balance_usdt()

        return {
            "mode": "PAPER" if self.fc.paper_trade else "LIVE",
            "connected": self.connected,
            "equity": round(equity, 2),
            "starting_equity": self.fc.paper_starting_equity if self.fc.paper_trade else None,
            "realized_pnl_today": round(self.realized_pnl_today, 4),
            "open_positions": [
                {
                    "symbol": p.symbol,
                    "side": p.side,
                    "agent": p.agent,
                    "entry_price": p.entry_price,
                    "quantity": p.quantity,
                    "notional": p.notional,
                    "leverage": p.leverage,
                    "sl_price": p.sl_price,
                    "tp_price": p.tp_price,
                    "peak_price": p.peak_price,
                    "entry_time": p.entry_time,
                    "funding_paid": round(p.funding_paid, 4),
                    "paper": p.paper,
                }
                for p in self.positions.values()
            ],
            "history": [asdict(t) for t in self.history[-30:]],
            "summary": {
                "total_trades": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
            },
        }

    # ─── Persistence helpers ────────────────────────────────────────────────

    def _save_positions(self):
        try:
            data = {sym: asdict(p) for sym, p in self.positions.items()}
            with open(POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"_save_positions failed: {e}")

    def _load_positions(self) -> Dict[str, FuturesPosition]:
        if not os.path.exists(POSITIONS_FILE):
            return {}
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            out = {}
            for sym, p in data.items():
                # filter only known fields
                allowed = {f for f in FuturesPosition.__dataclass_fields__}
                p_clean = {k: v for k, v in p.items() if k in allowed}
                out[sym] = FuturesPosition(**p_clean)
            if out:
                logger.info(f"FuturesTrader: restored {len(out)} open positions: {list(out.keys())}")
            return out
        except Exception as e:
            logger.warning(f"_load_positions failed: {e}")
            return {}

    def _save_history(self):
        try:
            data = [asdict(t) for t in self.history]
            with open(HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"_save_history failed: {e}")

    def _load_history(self) -> List[FuturesTradeRecord]:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE) as f:
                data = json.load(f)
            allowed = {f for f in FuturesTradeRecord.__dataclass_fields__}
            return [FuturesTradeRecord(**{k: v for k, v in t.items() if k in allowed}) for t in data]
        except Exception as e:
            logger.warning(f"_load_history failed: {e}")
            return []

    def _save_equity(self):
        try:
            with open(EQUITY_FILE, "w") as f:
                json.dump({
                    "paper_equity": self.paper_equity,
                    "realized_pnl_today": self.realized_pnl_today,
                    "today_iso": self._today_iso,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"_save_equity failed: {e}")

    def _load_equity(self):
        if not os.path.exists(EQUITY_FILE):
            return
        try:
            with open(EQUITY_FILE) as f:
                data = json.load(f)
            self.paper_equity = float(data.get("paper_equity", self.fc.paper_starting_equity))
            self.realized_pnl_today = float(data.get("realized_pnl_today", 0.0))
            self._today_iso = data.get("today_iso", self._today_iso)
        except Exception as e:
            logger.warning(f"_load_equity failed: {e}")
