"""
Celerity v2 — Executor (Binance Futures USDT-M)
================================================
Recibe órdenes ya decididas por TradingView y las ejecuta. NO calcula señales:
la estrategia vive en el Pine Script. Acá solo: validar riesgo, dimensionar,
abrir/cerrar, y registrar.

  - paper_trade=True  → simula (no toca Binance). Mantiene equity virtual.
  - paper_trade=False → órdenes MARKET reales en Futures.

Riesgo por trade: se arriesga risk_pct del equity. El tamaño sale del SL:
  qty = (equity * risk_pct) / (price * sl_pct/100)
acotado por min/max notional y por el equity disponible.
"""
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import market_data   # datos de precio por el mirror público (no geo-bloqueado)

logger = logging.getLogger("celerity.v2.executor")

FEE_TAKER = 0.0004  # 0.04% Futures taker


@dataclass
class Position:
    symbol: str
    side: str            # LONG / SHORT
    entry_price: float
    quantity: float
    notional: float
    leverage: int
    sl_price: float
    entry_time: str
    paper: bool
    order_id: str = ""


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    notional: float
    pnl_usdt: float
    pnl_pct: float
    fees: float
    reason: str
    hold_minutes: float
    entry_time: str
    exit_time: str
    paper: bool


class ExecutorV2:
    def __init__(self, cfg, telegram=None):
        self.cfg = cfg
        self.tg = telegram
        self.client = None
        self.connected = False
        self._lock = threading.Lock()
        self._symbol_info: Dict[str, dict] = {}

        os.makedirs(cfg.data_dir, exist_ok=True)
        self.pos_file = os.path.join(cfg.data_dir, "v2_positions.json")
        self.hist_file = os.path.join(cfg.data_dir, "v2_trades.json")
        self.eq_file = os.path.join(cfg.data_dir, "v2_equity.json")

        self.paper_equity = cfg.paper_starting_equity
        self.realized_pnl_today = 0.0
        self._today = datetime.now(timezone.utc).date().isoformat()

        self.positions: Dict[str, Position] = self._load_positions()
        self.history: List[TradeRecord] = self._load_history()
        self._load_equity()

    # ─── Conexión ────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        """
        Datos SIEMPRE por el mirror público (no bloqueado por ubicación).
        - Paper: no necesita cliente autenticado; arranca aunque el mirror tenga
          un hipo momentáneo (el loop reintenta) → nunca crashea el dashboard.
        - Live: además conecta el cliente de futures para poner ÓRDENES reales.
        """
        data_ok = market_data.reachable()
        if self.cfg.paper_trade:
            self.connected = True
            logger.info("ExecutorV2 conectado (PAPER · datos por mirror público · reachable=%s)", data_ok)
            return True
        # ── Live: hace falta cliente autenticado para las órdenes ──
        try:
            from binance.client import Client
            if not self.cfg.has_credentials:
                logger.error("Live sin claves de Binance — no puedo operar en real")
                return False
            self.client = Client(self.cfg.api_key, self.cfg.api_secret, testnet=self.cfg.testnet)
            self.client.futures_ping()
            self.connected = True
            logger.info("ExecutorV2 conectado (LIVE)")
            return True
        except Exception as e:
            logger.error("ExecutorV2 connect (LIVE) falló: %s", e)
            return False

    # ─── Precios / metadata ──────────────────────────────────────────────────
    def price(self, symbol: str) -> Optional[float]:
        # Live con cliente: precio de futures. Paper/fallback: mirror público.
        if not self.cfg.paper_trade and self.client:
            try:
                return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])
            except Exception as e:
                logger.debug("futures ticker %s falló, uso mirror: %s", symbol, e)
        try:
            return market_data.get_price(symbol)
        except Exception as e:
            logger.error("price %s (mirror): %s", symbol, e)
            return None

    def _info(self, symbol: str) -> dict:
        if symbol in self._symbol_info:
            return self._symbol_info[symbol]
        # Live con cliente: filtros reales de futures
        if not self.cfg.paper_trade and self.client:
            try:
                info = self.client.futures_exchange_info()
                for s in info["symbols"]:
                    if s["symbol"] == symbol:
                        f = {x["filterType"]: x for x in s["filters"]}
                        parsed = {
                            "step": float(f.get("LOT_SIZE", {}).get("stepSize", 0.001)),
                            "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0.001)),
                            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
                            "qty_prec": int(s.get("quantityPrecision", 3)),
                        }
                        self._symbol_info[symbol] = parsed
                        return parsed
            except Exception as e:
                logger.debug("futures exchange_info: %s", e)
        # Paper (o fallback): filtros del mirror público
        info = market_data.get_symbol_info(symbol)
        self._symbol_info[symbol] = info
        return info

    def _round_qty(self, symbol, qty):
        i = self._info(symbol)
        return round(qty - (qty % i["step"]), i["qty_prec"])

    # ─── Día / kill switch ───────────────────────────────────────────────────
    def _roll_day(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._today:
            logger.info("Rollover diario: %s → %s (PnL día reseteado)", self._today, today)
            self.realized_pnl_today = 0.0
            self._today = today

    def kill_switch_active(self) -> bool:
        self._roll_day()
        eq = self.equity()
        return self.realized_pnl_today <= -self.cfg.max_daily_loss_pct / 100.0 * eq

    def equity(self) -> float:
        if self.cfg.paper_trade:
            return self.paper_equity
        try:
            for b in self.client.futures_account_balance():
                if b["asset"] == "USDT":
                    return float(b["availableBalance"])
        except Exception:
            pass
        return self.paper_equity

    # ─── Orden principal (la llama el webhook) ───────────────────────────────
    def handle_signal(self, action: str, symbol: str, sl_pct: float = 0.0) -> dict:
        action = action.upper()
        symbol = symbol.upper()
        with self._lock:
            if action == "CLOSE":
                rec = self._close(symbol, reason="TV_CLOSE")
                return {"ok": bool(rec), "action": "CLOSE", "symbol": symbol,
                        "pnl": rec.pnl_usdt if rec else None}

            if action not in ("LONG", "SHORT"):
                return {"ok": False, "error": f"acción inválida: {action}"}

            # Reversión: si hay posición opuesta, cerrar primero
            if symbol in self.positions and self.positions[symbol].side != action:
                self._close(symbol, reason="TV_REVERSAL")

            if symbol in self.positions:
                return {"ok": False, "error": "ya hay posición en esa dirección"}

            if self.kill_switch_active():
                return {"ok": False, "error": "KILL SWITCH activo (pérdida diaria máxima)"}

            if len(self.positions) >= self.cfg.max_open_positions:
                return {"ok": False, "error": "máximo de posiciones alcanzado"}

            pos = self._open(symbol, action, sl_pct)
            if not pos:
                return {"ok": False, "error": "no se pudo abrir (ver logs)"}
            return {"ok": True, "action": action, "symbol": symbol,
                    "entry": pos.entry_price, "qty": pos.quantity,
                    "notional": round(pos.notional, 2), "sl": round(pos.sl_price, 6)}

    def _open(self, symbol, side, sl_pct) -> Optional[Position]:
        price = self.price(symbol)
        if not price:
            return None
        eq = self.equity()
        sl_pct = max(0.5, float(sl_pct or 3.0))   # piso de seguridad 0.5%

        # Sizing por riesgo: arriesgar risk_pct del equity dado el SL
        risk_amount = eq * self.cfg.risk_pct
        qty = risk_amount / (price * sl_pct / 100.0)
        notional = qty * price

        # Acotar por min/max notional y por equity (sin apalancar de más)
        notional = max(self.cfg.min_notional, min(self.cfg.max_notional, notional))
        notional = min(notional, eq * self.cfg.leverage * 0.95)
        qty = self._round_qty(symbol, notional / price)

        info = self._info(symbol)
        if qty < info["min_qty"] or qty * price < info["min_notional"]:
            qty = math.ceil(max(info["min_qty"], info["min_notional"] / price) / info["step"]) * info["step"]
            qty = round(qty, info["qty_prec"])
        if qty <= 0:
            logger.error("_open %s: qty=0", symbol)
            return None
        notional = qty * price

        sl_price = price * (1 - sl_pct/100) if side == "LONG" else price * (1 + sl_pct/100)
        order_id = ""

        if self.cfg.paper_trade:
            self.paper_equity -= notional * FEE_TAKER
            order_id = f"PAPER-{int(time.time()*1000)}"
        else:
            try:
                self.client.futures_change_leverage(symbol=symbol, leverage=self.cfg.leverage)
                o = self.client.futures_create_order(
                    symbol=symbol, side=("BUY" if side == "LONG" else "SELL"),
                    type="MARKET", quantity=qty)
                order_id = str(o.get("orderId", ""))
                price = self.price(symbol) or price
                sl_price = price * (1 - sl_pct/100) if side == "LONG" else price * (1 + sl_pct/100)
            except Exception as e:
                logger.error("_open %s LIVE falló: %s", symbol, e)
                return None

        pos = Position(symbol=symbol, side=side, entry_price=price, quantity=qty,
                       notional=notional, leverage=self.cfg.leverage, sl_price=sl_price,
                       entry_time=datetime.now(timezone.utc).isoformat(),
                       paper=self.cfg.paper_trade, order_id=order_id)
        self.positions[symbol] = pos
        self._save_positions()
        logger.info("%s ABIERTO %s %s qty=%s @ %.6f notional=%.2f SL=%.6f",
                    "📝" if pos.paper else "🔴", side, symbol, qty, price, notional, sl_price)
        self._notify(f"{'🟢' if side=='LONG' else '🔴'} {side} {symbol}\n"
                     f"Entry ${price:.4f} | Notional ${notional:.2f} | SL ${sl_price:.4f}\n"
                     f"Modo: {'PAPER' if pos.paper else 'LIVE'}")
        return pos

    def _close(self, symbol, reason) -> Optional[TradeRecord]:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        exit_price = self.price(symbol)
        if not exit_price:
            return None
        if not pos.paper:
            try:
                self.client.futures_create_order(
                    symbol=symbol, side=("SELL" if pos.side == "LONG" else "BUY"),
                    type="MARKET", quantity=pos.quantity, reduceOnly=True)
                exit_price = self.price(symbol) or exit_price
            except Exception as e:
                logger.error("_close %s LIVE falló: %s", symbol, e)
                return None

        gross = (exit_price - pos.entry_price) * pos.quantity if pos.side == "LONG" \
            else (pos.entry_price - exit_price) * pos.quantity
        fees = (pos.notional + exit_price * pos.quantity) * FEE_TAKER
        pnl = gross - fees
        pnl_pct = pnl / pos.notional * 100 if pos.notional else 0
        hold = (datetime.now(timezone.utc) -
                datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))).total_seconds() / 60

        if pos.paper:
            self.paper_equity += pnl
            self.realized_pnl_today += pnl

        rec = TradeRecord(symbol=symbol, side=pos.side, entry_price=pos.entry_price,
                          exit_price=exit_price, quantity=pos.quantity, notional=pos.notional,
                          pnl_usdt=round(pnl, 4), pnl_pct=round(pnl_pct, 3), fees=round(fees, 4),
                          reason=reason, hold_minutes=round(hold, 1), entry_time=pos.entry_time,
                          exit_time=datetime.now(timezone.utc).isoformat(), paper=pos.paper)
        self.history.append(rec)
        del self.positions[symbol]
        self._save_positions(); self._save_history(); self._save_equity()
        logger.info("%s CERRADO %s %s (%s) @ %.6f PnL $%+.4f (%.2f%%) hold %.0fmin",
                    "💰" if pnl >= 0 else "📉", pos.side, symbol, reason, exit_price, pnl, pnl_pct, hold)
        self._notify(f"{'💰' if pnl>=0 else '📉'} CLOSE {pos.side} {symbol} ({reason})\n"
                     f"PnL ${pnl:+.4f} ({pnl_pct:+.2f}%) | hold {hold:.0f}min")
        return rec

    # ─── Estado / persistencia ───────────────────────────────────────────────
    def status(self) -> dict:
        wins = sum(1 for t in self.history if t.pnl_usdt > 0)
        total = len(self.history)
        return {
            "mode": "PAPER" if self.cfg.paper_trade else "LIVE",
            "connected": self.connected,
            "equity": round(self.equity(), 2),
            "realized_pnl_today": round(self.realized_pnl_today, 4),
            "kill_switch": self.kill_switch_active(),
            "open_positions": [asdict(p) for p in self.positions.values()],
            "summary": {"trades": total, "wins": wins, "losses": total - wins,
                        "win_rate": round(wins / total * 100, 1) if total else 0,
                        "total_pnl": round(sum(t.pnl_usdt for t in self.history), 4)},
            "recent": [asdict(t) for t in self.history[-15:]],
        }

    def _notify(self, msg):
        if self.tg:
            try:
                self.tg(msg)
            except Exception:
                pass

    def _save_positions(self):
        json.dump({s: asdict(p) for s, p in self.positions.items()},
                  open(self.pos_file, "w"), indent=2)

    def _load_positions(self):
        if not os.path.exists(self.pos_file):
            return {}
        try:
            d = json.load(open(self.pos_file))
            allowed = set(Position.__dataclass_fields__)
            return {s: Position(**{k: v for k, v in p.items() if k in allowed}) for s, p in d.items()}
        except Exception:
            return {}

    def _save_history(self):
        json.dump([asdict(t) for t in self.history], open(self.hist_file, "w"), indent=2)

    def _load_history(self):
        if not os.path.exists(self.hist_file):
            return []
        try:
            allowed = set(TradeRecord.__dataclass_fields__)
            return [TradeRecord(**{k: v for k, v in t.items() if k in allowed})
                    for t in json.load(open(self.hist_file))]
        except Exception:
            return []

    def _save_equity(self):
        json.dump({"paper_equity": self.paper_equity, "realized_pnl_today": self.realized_pnl_today,
                   "today": self._today}, open(self.eq_file, "w"), indent=2)

    def _load_equity(self):
        if not os.path.exists(self.eq_file):
            return
        try:
            d = json.load(open(self.eq_file))
            self.paper_equity = float(d.get("paper_equity", self.paper_equity))
            self.realized_pnl_today = float(d.get("realized_pnl_today", 0.0))
            self._today = d.get("today", self._today)
        except Exception:
            pass
