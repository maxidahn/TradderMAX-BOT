"""
Celerity Trader Bot - Main Bot Engine (AI-Enhanced)
=====================================================
Two operating modes:

  CONSULTATION MODE (default):
    Bot analyzes → shows you the AI report → asks permission → you decide.
    Pending trades appear in the dashboard for approval/rejection.

  AUTONOMOUS MODE:
    Bot analyzes → executes automatically (after you trust it).
    Switch via dashboard toggle.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import pytz

from config import AppConfig
from strategy import Strategy, Signal
from trader import BinanceTrader
from tv_webhook import TradingViewReceiver
from telegram_notifier import TelegramNotifier

logger = logging.getLogger("celerity.bot")


class CelerityBot:
    """Main trading bot controller with AI and consultation mode."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.tv_receiver = TradingViewReceiver(config.tradingview)
        self.strategy = Strategy(
            config.strategy,
            costs_config=config.costs,
            tv_config=config.tradingview,
            tv_receiver=self.tv_receiver,
            app_config=config,
        )
        self.trader = BinanceTrader(config.binance, costs_config=config.costs)
        self.running = False
        self.paused = False
        self._thread = None
        self.last_signals: Dict[str, dict] = {}
        self.log_messages: List[dict] = []
        self._max_log_messages = 200

        # ─── Consultation Mode ───
        self.autonomous = False  # Start in consultation mode
        self.pending_trades: Dict[str, dict] = {}  # symbol -> trade proposal
        self._pending_lock = threading.Lock()
        self._log_lock = threading.Lock()

        # ─── Telegram ───
        self.telegram = TelegramNotifier()

    def _log(self, level: str, message: str):
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        with self._log_lock:
            self.log_messages.append(entry)
            if len(self.log_messages) > self._max_log_messages:
                self.log_messages = self.log_messages[-self._max_log_messages:]
        getattr(logger, level.lower() if level != "WARN" else "warning", logger.info)(message)

    def is_market_open(self) -> bool:
        tz = pytz.timezone(self.config.schedule.timezone)
        now = datetime.now(tz)
        if now.weekday() not in self.config.schedule.trading_days:
            return False
        market_open = now.replace(
            hour=self.config.schedule.market_open_hour,
            minute=self.config.schedule.market_open_minute, second=0,
        )
        market_close = now.replace(
            hour=self.config.schedule.market_close_hour,
            minute=self.config.schedule.market_close_minute, second=0,
        )
        return market_open <= now <= market_close

    def start(self) -> bool:
        if self.running:
            return False
        if not self.trader.connected:
            self._log("INFO", "Connecting to Binance...")
            if not self.trader.connect():
                self._log("ERROR", "Failed to connect. Check API keys.")
                return False
            mode = "TESTNET" if self.config.binance.testnet else "LIVE"
            self._log("INFO", f"Connected ({mode})")

        self.running = True
        self.paused = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        mode_str = "AUTONOMOUS" if self.autonomous else "CONSULTATION"
        self._log("INFO", f"Bot started in {mode_str} mode")
        self.telegram.bot_started(
            mode=mode_str,
            pairs=[p.symbol for p in self.config.trading_pairs if p.enabled],
        )

        # ─── Show restored session summary ───
        n_trades = len(self.trader.trade_history)
        n_positions = len(self.trader.positions)
        if n_trades > 0:
            total_pnl = sum(t.pnl for t in self.trader.trade_history)
            self._log("INFO", f"📂 Sesión restaurada: {n_trades} trades históricos | PnL acumulado: ${total_pnl:+.4f}")
        if n_positions > 0:
            self._log("INFO", f"⚠️  Posiciones abiertas restauradas: {list(self.trader.positions.keys())} — monitoreando SL/TP")

        return True

    def stop(self):
        self.running = False
        self._log("INFO", "Bot stopped")
        self.telegram.bot_stopped()

    def pause(self):
        self.paused = True
        self._log("INFO", "Bot paused")

    def resume(self):
        self.paused = False
        self._log("INFO", "Bot resumed")

    def set_autonomous(self, enabled: bool):
        """Switch between autonomous and consultation mode."""
        self.autonomous = enabled
        mode = "AUTONOMOUS" if enabled else "CONSULTATION"
        self._log("INFO", f"Switched to {mode} mode")

    def approve_trade(self, symbol: str) -> bool:
        """Approve a pending trade (consultation mode)."""
        with self._pending_lock:
            trade = self.pending_trades.pop(symbol, None)
        if not trade:
            return False

        if trade["action"] == "BUY":
            self._log("INFO", f"Placing BUY order for {symbol} (${trade['amount']:.2f})...")
            position = self.trader.place_buy(symbol, trade["amount"])
            if position:
                self._log("INFO", f"✅ COMPRADO: {symbol} @ ${position.entry_price:.2f} | Qty: {position.quantity}")
                self.telegram.buy_executed(
                    symbol=symbol, price=position.entry_price,
                    quantity=position.quantity, amount=trade["amount"],
                    ai_score=trade.get("ai_score", 0),
                )
                return True
            else:
                self._log("ERROR", f"❌ ORDEN FALLIDA: {symbol} — revisa el Activity Log o los logs de terminal")
                return False
        elif trade["action"] == "SELL":
            self._log("INFO", f"Placing SELL order for {symbol}...")
            record = self.trader.place_sell(symbol, reason=trade.get("reason", "Approved"))
            if record:
                self._log("INFO", f"✅ VENDIDO: {symbol} @ ${record.price:.2f} | PnL: ${record.pnl:.4f}")
                self.telegram.sell_executed(
                    symbol=symbol, price=record.price,
                    pnl=record.pnl, pnl_pct=record.pnl_pct,
                    reason="Approved",
                )
                return True
            else:
                self._log("ERROR", f"❌ VENTA FALLIDA: {symbol} — revisa los logs de terminal")
                return False
        return False

    def reject_trade(self, symbol: str) -> bool:
        """Reject a pending trade."""
        with self._pending_lock:
            trade = self.pending_trades.pop(symbol, None)
        if trade:
            self._log("INFO", f"REJECTED: {trade['action']} {symbol}")
            self.telegram.buy_rejected(symbol)
            return True
        return False

    def close_position(self, symbol: str) -> dict:
        """Manually close an open position."""
        if symbol not in self.trader.positions:
            return {"success": False, "error": f"No open position for {symbol}"}
        pos = self.trader.positions[symbol]
        self._log("INFO", f"🔴 CIERRE MANUAL: {symbol} (entrada @ ${pos.entry_price:.2f})")
        record = self.trader.place_sell(symbol, reason="Manual close")
        if record:
            self._log("INFO",
                f"✅ CERRADO MANUALMENTE: {symbol} @ ${record.price:.2f} | "
                f"PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
            self.telegram.sell_executed(
                symbol=symbol, price=record.price,
                pnl=record.pnl, pnl_pct=record.pnl_pct,
                reason="Manual close",
            )
            return {"success": True, "price": record.price, "pnl": record.pnl, "pnl_pct": record.pnl_pct}
        self._log("ERROR", f"❌ CIERRE FALLIDO: {symbol}")
        return {"success": False, "error": "Order failed — check logs"}

    # ─── Fast SL/TP monitor (runs every 15 s, no heavy AI calls) ────────────
    SL_TP_INTERVAL = 15   # seconds between fast price checks
    AI_INTERVAL    = 60   # seconds between full AI analysis cycles

    def _run_loop(self):
        self._log("INFO", "AI Trading loop started (SL/TP: 15s · AI analysis: 60s)")
        self._last_market_closed_log = 0
        self._last_ai_analysis = 0.0   # timestamp of last full AI pass

        while self.running:
            try:
                now_ts = time.time()

                if self.config.restrict_to_market_hours and not self.is_market_open():
                    if now_ts - self._last_market_closed_log >= 600:
                        tz = pytz.timezone(self.config.schedule.timezone)
                        now = datetime.now(tz)
                        self._log("INFO", f"Market closed ({now.strftime('%A %H:%M ET')}). Waiting...")
                        self._last_market_closed_log = now_ts
                    time.sleep(60)
                    continue

                if self.paused:
                    time.sleep(self.SL_TP_INTERVAL)
                    continue

                # ── Fast path: lightweight SL/TP check on every tick ──────────
                if self.trader.positions:
                    self._fast_sl_tp_check()

                # ── Slow path: full AI analysis (once per AI_INTERVAL) ────────
                if now_ts - self._last_ai_analysis >= self.AI_INTERVAL:
                    for pair in self.config.trading_pairs:
                        if not pair.enabled or not self.running:
                            continue
                        self._process_pair(pair)
                    self._last_ai_analysis = time.time()

                time.sleep(self.SL_TP_INTERVAL)

            except Exception as e:
                self._log("ERROR", f"Loop error: {e}")
                time.sleep(10)

    def _fast_sl_tp_check(self):
        """
        Lightweight SL/TP monitor — runs every 15 s.
        Only fetches the current ticker price (single API call per position).
        No candle downloads, no AI analysis.
        """
        risk_params = self.config.get_risk_params()
        sl_pct = risk_params.get("stop_loss_pct", self.config.strategy.stop_loss_pct)
        tp_pct = risk_params.get("take_profit_pct", self.config.strategy.take_profit_pct)

        for symbol, pos in list(self.trader.positions.items()):
            try:
                # Single lightweight ticker call — much cheaper than full candle fetch
                ticker = self.trader.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker["price"])

                sl_tp = self.strategy.check_stop_loss_take_profit(
                    pos.entry_price, current_price, pos.side, sl_pct, tp_pct
                )

                if sl_tp:
                    self._log("INFO", f"⚡ {symbol}: {sl_tp} triggered @ ${current_price:.4f} (fast check)")
                    record = self.trader.place_sell(symbol, reason=sl_tp)
                    if record:
                        self._log("INFO",
                            f"{symbol}: Closed @ ${record.price:.4f} | "
                            f"PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                        if "STOP" in sl_tp:
                            self.telegram.stop_loss(symbol, record.price, record.pnl, record.pnl_pct)
                        else:
                            self.telegram.take_profit(symbol, record.price, record.pnl, record.pnl_pct)
            except Exception as e:
                logger.debug(f"Fast SL/TP check error for {symbol}: {e}")

    def _process_pair(self, pair):
        try:
            df = self.trader.get_candles(
                symbol=pair.symbol,
                interval=self.config.strategy.timeframe,
                limit=self.config.strategy.lookback_candles,
            )
            if df is None or df.empty:
                return

            # Run full AI analysis
            result = self.strategy.analyze(df, pair.symbol)
            self._log("INFO", f"{pair.symbol}: Analysis done — signal={result.signal.value}, score={result.ai_score:.3f}, price={result.price:.2f}")

            # Fetch current spread for dashboard display
            spread_data = {}
            try:
                spread_info = self.trader.get_spread(pair.symbol)
                if spread_info:
                    spread_data = {
                        "bid": round(spread_info.bid, 2),
                        "ask": round(spread_info.ask, 2),
                        "spread_pct": spread_info.spread_pct,
                        "mid_price": round(spread_info.mid_price, 2),
                    }
            except Exception as spread_err:
                self._log("WARN", f"{pair.symbol}: spread fetch error: {spread_err}")

            # Cost estimate for this trade
            cost_drag = self.config.costs.round_trip_fee_pct + self.config.costs.slippage_base_pct

            # Current threshold for dashboard display (matches strategy: no extra cost penalty)
            risk_params = self.config.get_risk_params()
            current_threshold = round(risk_params.get("signal_threshold", 0.25), 3)

            # Store for dashboard (with AI data + costs)
            self.last_signals[pair.symbol] = {
                "signal": result.signal.value,
                "price": round(result.price, 2),
                "rsi": round(result.rsi, 1),
                "ema_fast": round(result.ema_fast, 2),
                "ema_slow": round(result.ema_slow, 2),
                "volume_ratio": round(result.volume_ratio, 2),
                "reason": result.reason,
                "confidence": round(result.confidence, 2),
                "time": datetime.now().strftime("%H:%M:%S"),
                # AI-specific
                "ai_score": result.ai_score,
                "threshold": current_threshold,
                "regime": result.regime,
                "sentiment": result.sentiment_label,
                "ml_prediction": result.ml_prediction,
                "explanation": result.explanation,
                "insights": [
                    {
                        "source": i.source,
                        "score": round(i.score, 3),
                        "signal": i.signal,
                        "confidence": round(i.confidence, 2),
                        "details": i.details,
                    }
                    for i in result.insights
                ],
                # Transaction costs
                "spread": spread_data,
                "cost_drag_pct": round(cost_drag, 3),
                "fee_rate": self.config.costs.effective_fee_rate * 100,
                # TradingView
                "tv_signal": result.tv_signal,
                "tv_consensus": self.tv_receiver.get_consensus(pair.symbol),
            }
            self._log("INFO", f"{pair.symbol}: Signal stored → dashboard updated")

            # Check SL/TP for open positions
            if pair.symbol in self.trader.positions:
                pos = self.trader.positions[pair.symbol]
                # Use risk_level SL/TP (source of truth shown on dashboard)
                # Adaptive values are used as fallback only
                adapted_for_sl = self.strategy.adaptive.adapt_parameters(df, pair.symbol)
                risk_params_sl  = self.config.get_risk_params()
                sl_pct = risk_params_sl.get("stop_loss_pct",   adapted_for_sl.stop_loss_pct)
                tp_pct = risk_params_sl.get("take_profit_pct", adapted_for_sl.take_profit_pct)
                sl_tp = self.strategy.check_stop_loss_take_profit(
                    pos.entry_price, result.price, pos.side,
                    sl_pct, tp_pct,
                )
                if sl_tp:
                    self._log("INFO", f"{pair.symbol}: {sl_tp} triggered!")
                    # SL/TP always execute immediately (safety)
                    record = self.trader.place_sell(pair.symbol, reason=sl_tp)
                    if record:
                        self._log("INFO",
                            f"{pair.symbol}: Closed @ ${record.price:.2f} | PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                        if "STOP" in sl_tp:
                            self.telegram.stop_loss(pair.symbol, record.price, record.pnl, record.pnl_pct)
                        else:
                            self.telegram.take_profit(pair.symbol, record.price, record.pnl, record.pnl_pct)
                    return

            # ─── Adjust slippage estimate based on regime ───
            if result.regime == "VOLATILE":
                self.config.costs.slippage_base_pct = self.config.costs.slippage_volatile_pct
            elif result.regime == "QUIET":
                self.config.costs.slippage_base_pct = self.config.costs.slippage_quiet_pct
            else:
                self.config.costs.slippage_base_pct = 0.05  # Default

            # ─── Execute or Consult ───
            if result.signal == Signal.BUY and pair.symbol not in self.trader.positions:
                adapted = self.strategy.adaptive.adapt_parameters(df, pair.symbol)
                risk_params = self.config.get_risk_params()
                # Risk level controls overall size; adaptive regime fine-tunes within ±30%
                regime_adj = max(0.7, min(1.3, adapted.position_size_multiplier))
                amount = pair.trade_amount_usdt * risk_params.get("position_size_mult", 1.0) * regime_adj
                amount = max(pair.min_trade_usdt, min(pair.max_trade_usdt, amount))

                if self.autonomous:
                    self._log("INFO", f"{pair.symbol}: AUTO BUY (AI score: {result.ai_score:+.3f})")
                    position = self.trader.place_buy(pair.symbol, amount)
                    if position:
                        self._log("INFO", f"{pair.symbol}: Bought @ ${position.entry_price:.2f} (${amount:.1f})")
                        self.telegram.buy_executed(
                            symbol=pair.symbol, price=position.entry_price,
                            quantity=position.quantity, amount=amount,
                            ai_score=result.ai_score,
                        )
                else:
                    # Consultation mode: propose trade
                    with self._pending_lock:
                        self.pending_trades[pair.symbol] = {
                            "action": "BUY",
                            "symbol": pair.symbol,
                            "amount": round(amount, 2),
                            "price": result.price,
                            "ai_score": result.ai_score,
                            "confidence": result.confidence,
                            "explanation": result.explanation,
                            "reason": result.reason,
                            "time": datetime.now().strftime("%H:%M:%S"),
                        }
                    self._log("INFO", f"{pair.symbol}: BUY proposed (AI: {result.ai_score:+.3f}) — awaiting your approval")
                    self.telegram.buy_proposed(
                        symbol=pair.symbol, amount=round(amount, 2),
                        price=result.price, ai_score=result.ai_score,
                        reason=result.reason,
                    )

            elif result.signal == Signal.SELL and pair.symbol in self.trader.positions:
                # SELLs are always automatic — waiting for approval risks capital in fast markets
                self._log("INFO", f"{pair.symbol}: AUTO SELL (AI score: {result.ai_score:+.3f})")
                record = self.trader.place_sell(pair.symbol, reason="AI Signal")
                if record:
                    self._log("INFO", f"✅ VENDIDO: {pair.symbol} @ ${record.price:.2f} | PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                    self.telegram.sell_executed(
                        symbol=pair.symbol, price=record.price,
                        pnl=record.pnl, pnl_pct=record.pnl_pct,
                        reason="AI Signal",
                    )
                else:
                    self._log("ERROR", f"❌ VENTA FALLIDA: {pair.symbol} — revisa los logs")
                if False:  # kept for reference, consultation SELL no longer used
                    with self._pending_lock:
                        self.pending_trades[pair.symbol] = {
                            "action": "SELL",
                            "symbol": pair.symbol,
                            "price": result.price,
                            "ai_score": result.ai_score,
                            "confidence": result.confidence,
                            "explanation": result.explanation,
                            "reason": result.reason,
                            "time": datetime.now().strftime("%H:%M:%S"),
                        }

        except Exception as e:
            self._log("ERROR", f"{pair.symbol}: {e}")

    def _get_recent_logs(self, count: int) -> List[dict]:
        """Thread-safe access to recent log messages."""
        with self._log_lock:
            return list(self.log_messages[-count:])

    def get_full_status(self) -> Dict:
        tz = pytz.timezone(self.config.schedule.timezone)
        now = datetime.now(tz)

        with self._pending_lock:
            pending = dict(self.pending_trades)

        return {
            "bot": {
                "running": self.running,
                "paused": self.paused,
                "autonomous": self.autonomous,
                "market_open": True if not self.config.restrict_to_market_hours else self.is_market_open(),
                "current_time_et": now.strftime("%A %H:%M:%S ET"),
                "mode": "TESTNET" if self.config.binance.testnet else "LIVE",
                "risk_level": self.config.risk_level,
                "risk_params": self.config.get_risk_params(),
            },
            "signals": self.last_signals,
            "pending_trades": pending,
            "trader": self.trader.get_status(),
            "account": self.trader.get_account_info() if self.trader.connected else {
                "error": f"Not connected — {self.config.binance.credentials_status}"
            },
            "pairs": [
                {
                    "symbol": p.symbol,
                    "name": p.name,
                    "enabled": p.enabled,
                    "trade_amount": p.trade_amount_usdt,
                }
                for p in self.config.trading_pairs
            ],
            "tradingview": self.tv_receiver.get_stats(),
            "logs": self._get_recent_logs(50),
        }
