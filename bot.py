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
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pytz

import persistence
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

        # ─── Proxy de mercado: régimen BTC como filtro de correlación ───────
        # Se actualiza en cada ciclo al analizar BTCUSDC.
        # Bloquea entradas en altcoins cuando BTC está en riesgo.
        self._market_regime: str    = "UNKNOWN"   # VOLATILE / TRENDING / RANGING / QUIET
        self._market_direction: str = "neutral"   # "up" / "down" / "neutral"
        self._btc_tech_score: float = 0.0          # BTC Technical layer score — used to gate altcoin entries
        self._btc_rsi: float        = 50.0         # BTC RSI actual — si está overbought, bloquea altcoins también
        self._consecutive_sell: dict = {}          # debounce: counts consecutive SELL signals per symbol

        # ─── BUY cooldown: evita reintentos repetidos por par ────────────────
        # Registra el timestamp del último intento de BUY (exitoso o fallido).
        # Si el intento fue hace menos de BUY_COOLDOWN_SEC, se bloquea el nuevo intento.
        self._buy_last_attempt: Dict[str, float] = {}
        self.BUY_COOLDOWN_SEC = 5 * 60  # 5 minutos de cooldown entre intentos de BUY

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
                    # Even when paused we must protect open positions — SL/TP still runs
                    if self.trader.positions:
                        self._fast_sl_tp_check()
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
        Lightweight position monitor — runs every 15 s.
        Checks SL/TP, trailing stop, and position timeout using real-time
        ticker price (single API call per position, no candle downloads).
        """
        # ── BUG 1 FIX: guard against disconnected client ─────────────────────
        if not self.trader.connected or not self.trader.client:
            self._log("WARN", "Fast SL/TP check skipped: not connected to Binance")
            return

        risk_params = self.config.get_risk_params()
        sl_pct      = risk_params.get("stop_loss_pct",   self.config.strategy.stop_loss_pct)
        tp_pct      = risk_params.get("take_profit_pct", self.config.strategy.take_profit_pct)
        trail_pct   = self.config.strategy.trailing_stop_pct   # 0.0 = disabled
        max_hold_h  = self.config.strategy.max_hold_hours      # 0.0 = disabled

        for symbol, pos in list(self.trader.positions.items()):
            try:
                # Single lightweight ticker call — much cheaper than full candle fetch
                ticker = self.trader.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker["price"])

                # ── Update trailing stop peak price ───────────────────────────
                if trail_pct > 0 and current_price > pos.peak_price:
                    pos.peak_price = current_price
                    persistence.save_open_positions(self.trader.positions)

                # ── 1. Standard SL/TP check (with Partial TP support) ─────────
                sl_tp = self.strategy.check_stop_loss_take_profit(
                    pos.entry_price, current_price, pos.side, sl_pct, tp_pct,
                    partial_tp_taken=pos.partial_tp_taken,
                )
                if sl_tp == "PARTIAL_TP":
                    self._log("INFO", f"🎯 {symbol}: PARTIAL_TP @ ${current_price:.4f} — cerrando 50%")
                    record = self.trader.place_partial_sell(symbol, fraction=0.5)
                    if record:
                        self._log("INFO",
                            f"{symbol}: Partial TP @ ${record.price:.4f} | "
                            f"PnL 50%: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%) | "
                            f"Restante qty: {self.trader.positions[symbol].quantity:.6f}")
                        self.telegram.take_profit(symbol, record.price, record.pnl, record.pnl_pct)
                    # Position still open (half remains) — continue to trailing/timeout checks
                elif sl_tp:
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
                    continue  # Position closed — skip trailing/timeout for this symbol

                # ── 2. Trailing stop — only activates after position is in profit
                # (peak must be ≥ 0.5% above entry to cover fees before trailing fires)
                peak_gain_pct = (pos.peak_price - pos.entry_price) / pos.entry_price * 100
                if trail_pct > 0 and peak_gain_pct >= 0.5:
                    drawdown_pct = (pos.peak_price - current_price) / pos.peak_price * 100
                    if drawdown_pct >= trail_pct:
                        self._log("INFO",
                            f"🔻 {symbol}: TRAILING_STOP — "
                            f"peak ${pos.peak_price:.4f} → ${current_price:.4f} "
                            f"(caída: {drawdown_pct:.2f}% ≥ {trail_pct}%)")
                        record = self.trader.place_sell(symbol, reason="TRAILING_STOP")
                        if record:
                            self._log("INFO",
                                f"{symbol}: Trailing stop @ ${record.price:.4f} | "
                                f"PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                            self.telegram.stop_loss(symbol, record.price, record.pnl, record.pnl_pct)
                        continue

                # ── 3. Timeout dinámico: posición en pérdida continua ─────────
                # Si la posición lleva ≥ LOSS_TIMEOUT horas y el precio NUNCA superó
                # un 0.1% por encima de la entrada (peak ≈ entry), cierra a mercado.
                # Evita quedar atrapado esperando hasta las 24h del timeout estándar.
                LOSS_TIMEOUT_H = 4.0   # horas antes de cerrar posición que nunca subió
                PEAK_THRESHOLD = 1.001  # peak debe haber sido al menos +0.1% para NO cerrar
                if pos.entry_time:
                    try:
                        entry_dt_lt  = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                        hold_hours_lt = (datetime.now(timezone.utc) - entry_dt_lt).total_seconds() / 3600
                        never_moved_up = pos.peak_price <= pos.entry_price * PEAK_THRESHOLD
                        if hold_hours_lt >= LOSS_TIMEOUT_H and never_moved_up:
                            self._log("INFO",
                                f"🔴 {symbol}: LOSS_TIMEOUT — {hold_hours_lt:.1f}h abierta sin subir "
                                f"(peak ${pos.peak_price:.4f} ≈ entrada ${pos.entry_price:.4f}) — cerrando")
                            record = self.trader.place_sell(symbol, reason="LOSS_TIMEOUT")
                            if record:
                                self._log("INFO",
                                    f"{symbol}: Cerrada por LOSS_TIMEOUT @ ${record.price:.4f} | "
                                    f"PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                                self.telegram.sell_executed(
                                    symbol=symbol, price=record.price,
                                    pnl=record.pnl, pnl_pct=record.pnl_pct,
                                    reason="LOSS_TIMEOUT",
                                )
                            continue
                    except Exception as lt_err:
                        logger.debug(f"LOSS_TIMEOUT check failed for {symbol}: {lt_err}")

                # ── 4. Position timeout — close stale trades ──────────────────
                if max_hold_h > 0 and pos.entry_time:
                    try:
                        entry_dt   = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                        hold_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                        if hold_hours >= max_hold_h:
                            self._log("INFO",
                                f"⏰ {symbol}: TIMEOUT — abierto {hold_hours:.1f}h "
                                f"(máx {max_hold_h}h) — cerrando a mercado")
                            record = self.trader.place_sell(symbol, reason="TIMEOUT")
                            if record:
                                self._log("INFO",
                                    f"{symbol}: Timeout cerrado @ ${record.price:.4f} | "
                                    f"PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                                self.telegram.sell_executed(
                                    symbol=symbol, price=record.price,
                                    pnl=record.pnl, pnl_pct=record.pnl_pct,
                                    reason="TIMEOUT",
                                )
                    except Exception as te:
                        logger.debug(f"Timeout check failed for {symbol}: {te}")

            except Exception as e:
                # ── BUG 1 FIX: was logger.debug — invisible at INFO level ─────
                logger.warning(f"Fast SL/TP check error for {symbol}: {e}")

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

            # ── Regime-adjusted slippage — local var, never mutates shared config ──
            # BUG 5 FIX: computing this early so cost_drag shown on dashboard is accurate
            if result.regime == "VOLATILE":
                _regime_slippage = self.config.costs.slippage_volatile_pct
            elif result.regime == "QUIET":
                _regime_slippage = self.config.costs.slippage_quiet_pct
            else:
                _regime_slippage = self.config.costs.slippage_base_pct

            # Cost estimate for this trade (regime-adjusted for accurate dashboard display)
            cost_drag = self.config.costs.round_trip_fee_pct + _regime_slippage

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
                # Transaction costs (cost_drag is refined by regime below)
                "spread": spread_data,
                "cost_drag_pct": round(cost_drag, 3),
                "fee_rate": self.config.costs.effective_fee_rate * 100,
                # TradingView
                "tv_signal": result.tv_signal,
                "tv_consensus": self.tv_receiver.get_consensus(pair.symbol),
            }
            self._log("INFO", f"{pair.symbol}: Signal stored → dashboard updated")

            # ─── Adaptive params — se calculan una sola vez por ciclo ─────────
            # También actualiza el proxy de mercado BTC para el filtro de correlación
            adapted = self.strategy.adaptive.adapt_parameters(df, pair.symbol)
            if pair.symbol.startswith("BTC"):
                self._market_regime    = result.regime
                self._market_direction = getattr(adapted, "trend_direction", "neutral")
                # Capture BTC Technical score to gate altcoin BUYs
                btc_tech = next((ins.score for ins in result.insights if ins.source == "Technical"), None)
                if btc_tech is not None:
                    self._btc_tech_score = btc_tech
                # Capture BTC RSI — si está overbought bloquea altcoins también
                self._btc_rsi = result.rsi
                if self._market_regime != "UNKNOWN":
                    self._log("INFO",
                        f"📊 Proxy BTC: {self._market_regime} "
                        f"({'↑' if self._market_direction == 'up' else '↓' if self._market_direction == 'down' else '→'}) "
                        f"| Tech: {self._btc_tech_score:+.2f} | RSI: {self._btc_rsi:.1f}")

            # Check SL/TP for open positions
            if pair.symbol in self.trader.positions:
                pos = self.trader.positions[pair.symbol]
                # Use risk_level SL/TP (source of truth shown on dashboard)
                risk_params_sl = self.config.get_risk_params()
                sl_pct = risk_params_sl.get("stop_loss_pct",   adapted.stop_loss_pct)
                tp_pct = risk_params_sl.get("take_profit_pct", adapted.take_profit_pct)
                sl_tp = self.strategy.check_stop_loss_take_profit(
                    pos.entry_price, result.price, pos.side,
                    sl_pct, tp_pct,
                    partial_tp_taken=pos.partial_tp_taken,
                )
                if sl_tp == "PARTIAL_TP":
                    self._log("INFO", f"🎯 {pair.symbol}: PARTIAL_TP triggered — cerrando 50%")
                    record = self.trader.place_partial_sell(pair.symbol, fraction=0.5)
                    if record:
                        self._log("INFO",
                            f"{pair.symbol}: Partial TP @ ${record.price:.4f} | "
                            f"PnL 50%: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                        self.telegram.take_profit(pair.symbol, record.price, record.pnl, record.pnl_pct)
                    # No return — position still half open, continue to signal check
                elif sl_tp:
                    self._log("INFO", f"{pair.symbol}: {sl_tp} triggered!")
                    record = self.trader.place_sell(pair.symbol, reason=sl_tp)
                    if record:
                        self._log("INFO",
                            f"{pair.symbol}: Closed @ ${record.price:.2f} | PnL: ${record.pnl:.4f} ({record.pnl_pct:+.2f}%)")
                        if "STOP" in sl_tp:
                            self.telegram.stop_loss(pair.symbol, record.price, record.pnl, record.pnl_pct)
                        else:
                            self.telegram.take_profit(pair.symbol, record.price, record.pnl, record.pnl_pct)
                    return

            # ─── Execute or Consult ───
            # Reset consecutive SELL counter when signal is not SELL
            if result.signal != Signal.SELL:
                self._consecutive_sell.pop(pair.symbol, None)

            if result.signal == Signal.BUY and pair.symbol not in self.trader.positions:
                risk_params = self.config.get_risk_params()

                # ══ CIRCUIT BREAKER: pérdida diaria máxima ════════════════════
                # Si perdemos demasiado hoy, pausamos nuevas entradas hasta mañana.
                # Evita el "tilt" — seguir operando en un mercado que nos va en contra.
                MAX_DAILY_LOSS = -1.50   # USD — ajustable según capital
                try:
                    daily_stats = self.trader.get_daily_stats()
                    daily_pnl   = daily_stats.get("pnl", 0.0)
                    if daily_pnl < MAX_DAILY_LOSS:
                        self._log("INFO",
                            f"{pair.symbol}: BUY bloqueado — CIRCUIT BREAKER: "
                            f"pérdida diaria ${daily_pnl:.2f} excede límite ${MAX_DAILY_LOSS}")
                        return
                except Exception:
                    pass

                # ══ MARKET BREADTH: mercado mayoritariamente bajista ══════════
                # Si ≥ 55% de los pares monitoreados tienen score negativo → no entrar.
                # Una señal alcista aislada en un mercado bajista suele fallar.
                try:
                    all_scores   = [v.get("ai_score", 0) for v in self.last_signals.values()]
                    neg_count    = sum(1 for s in all_scores if s < 0)
                    total_count  = len(all_scores)
                    breadth_bear = total_count > 0 and neg_count / total_count >= 0.55
                    if breadth_bear:
                        self._log("INFO",
                            f"{pair.symbol}: BUY bloqueado — BREADTH BAJISTA: "
                            f"{neg_count}/{total_count} pares con score negativo")
                        return
                except Exception:
                    pass

                # ── Cooldown: evitar reintentos rápidos del mismo par ─────────
                now_ts = time.time()
                last_attempt = self._buy_last_attempt.get(pair.symbol, 0.0)
                elapsed = now_ts - last_attempt
                if elapsed < self.BUY_COOLDOWN_SEC:
                    remaining = int(self.BUY_COOLDOWN_SEC - elapsed)
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — cooldown activo ({remaining}s restantes desde último intento)")
                    return

                # ── Límite de posiciones simultáneas ─────────────────────────
                max_pos = self.config.strategy.max_open_positions
                current_pos = len(self.trader.positions)
                if current_pos >= max_pos:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — máx {max_pos} posiciones "
                        f"({current_pos} abiertas actualmente)")
                    return

                # ── Filtro de correlación BTC ─────────────────────────────────
                if not pair.symbol.startswith("BTC") and self._market_regime != "UNKNOWN":
                    if self._market_regime == "VOLATILE":
                        self._log("INFO", f"{pair.symbol}: BUY bloqueado — BTC VOLATILE")
                        return
                    if self._market_regime == "TRENDING" and self._market_direction == "down":
                        self._log("INFO", f"{pair.symbol}: BUY bloqueado — BTC bajista ↓")
                        return
                    # BTC Technical bearish → no altcoin longs
                    if self._btc_tech_score < -0.10:
                        self._log("INFO",
                            f"{pair.symbol}: BUY bloqueado — BTC Technical negativo ({self._btc_tech_score:+.2f})")
                        return
                    # ── BTC RSI overbought → no altcoin longs ─────────────────
                    # Si BTC tiene RSI alto y estaría bloqueado por overbought,
                    # los altcoins entran justo en el peor momento (BTC a punto de corregir).
                    # Usamos el mismo techo que el par BTC para consistencia.
                    BTC_RSI_MAX = 63.0
                    if self._btc_rsi > BTC_RSI_MAX:
                        self._log("INFO",
                            f"{pair.symbol}: BUY bloqueado — BTC RSI overbought "
                            f"({self._btc_rsi:.1f} > {BTC_RSI_MAX}) → riesgo de corrección")
                        return

                # ── Filtro de volumen mínimo ──────────────────────────────────
                # volume_ratio ya usa velas completadas (completed_vol_ratio)
                VOL_MIN = 0.30
                if result.volume_ratio < VOL_MIN:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — volumen {result.volume_ratio:.2f}x < {VOL_MIN}x (sin liquidez)")
                    return

                # ── Filtro RSI techo (no entrar en overbought) ────────────────
                RSI_BUY_MAX = 63.0   # 60 era demasiado restrictivo — RSI 60-63 no es overbought real
                if result.rsi > RSI_BUY_MAX:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — RSI {result.rsi:.1f} > {RSI_BUY_MAX} (overbought)")
                    return

                # ── Filtro Adaptive: régimen propio del par debe ser alcista ──
                # El Adaptive layer detecta el régimen del par individualmente.
                # Si está en tendencia bajante o rango sin dirección, no entrar.
                adaptive_score = next(
                    (ins.score for ins in result.insights if ins.source == "Adaptive"), None
                )
                if adaptive_score is not None and adaptive_score <= 0:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — Adaptive score {adaptive_score:+.3f} "
                        f"≤ 0 (régimen no alcista)")
                    return

                # ── Filtro de calidad de señal técnica ───────────────────────
                # Requiere confirmación mínima del análisis técnico (EMA+RSI+Vol).
                # Evita entrar basándonos solo en ML o Adaptive sin momentum real.
                tech_score = next(
                    (ins.score for ins in result.insights if ins.source == "Technical"), None
                )
                MIN_TECH_SCORE = 0.12
                if tech_score is not None and tech_score < MIN_TECH_SCORE:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — Technical score {tech_score:.3f} "
                        f"< {MIN_TECH_SCORE} (sin momentum técnico suficiente)")
                    return

                # ── Bloqueo duro: precio demasiado extendido sobre EMA ───────
                # Si el precio ya subió > 1.2% por encima del EMA rápido, el tren
                # ya salió. Entrar aquí = comprar el pico y esperar el pullback.
                # El análisis técnico usa la última vela cerrada, pero la orden se
                # ejecuta al precio ACTUAL que puede ser incluso más alto.
                ema_fast_val = result.ema_fast
                live_price   = result.price   # precio de la última vela cerrada
                if ema_fast_val > 0:
                    price_ext = (live_price - ema_fast_val) / ema_fast_val * 100
                    PRICE_EXT_MAX = 1.2  # bloquea si precio > 1.2% sobre EMA9
                    if price_ext > PRICE_EXT_MAX:
                        self._log("INFO",
                            f"{pair.symbol}: BUY bloqueado — precio extendido "
                            f"+{price_ext:.2f}% sobre EMA9 (máx {PRICE_EXT_MAX}%)")
                        return

                # ── Sizing dinámico: % del capital disponible ─────────────────
                # Se ajusta automáticamente conforme crece (o decrece) el balance
                quote_asset  = self.trader._get_quote_asset(pair.symbol)
                available    = self.trader.get_balance(quote_asset)
                position_pct = risk_params.get("position_pct_capital", 0.05)
                regime_adj   = max(0.7, min(1.3, adapted.position_size_multiplier))
                amount = available * position_pct * regime_adj

                # ── Verificar que el capital alcanza para el mínimo del par ──
                # Evita el error "Quantity too small" de Binance (ej. BTC min $85)
                if amount < pair.min_trade_usdt:
                    self._log("INFO",
                        f"{pair.symbol}: BUY bloqueado — sizing dinámico ${amount:.1f} "
                        f"< mínimo del par ${pair.min_trade_usdt} (capital insuficiente)")
                    return

                # Respetar min/max del par (guardarraíles de seguridad)
                amount = max(pair.min_trade_usdt, min(pair.max_trade_usdt, amount))
                self._log("INFO",
                    f"{pair.symbol}: Sizing dinámico — balance:{available:.1f} "
                    f"× {position_pct:.1%} × régimen:{regime_adj:.2f}x = ${amount:.2f}")

                if self.autonomous:
                    # Registrar timestamp ANTES del intento para que el cooldown aplique
                    # incluso si la orden falla (evita martillar el mismo par en pérdida)
                    self._buy_last_attempt[pair.symbol] = time.time()
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
                    self._buy_last_attempt[pair.symbol] = time.time()
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
                # ── Tiempo mínimo de hold antes de AI Signal SELL ─────────────
                # Evita salir en el primer retroceso dentro de los primeros 30 min
                pos = self.trader.positions[pair.symbol]
                try:
                    entry_dt   = datetime.fromisoformat(pos.entry_time.replace("Z", "+00:00"))
                    hold_min   = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
                    MIN_HOLD_MIN = 30.0
                    if hold_min < MIN_HOLD_MIN:
                        self._log("INFO",
                            f"{pair.symbol}: SELL bloqueado — posición abierta hace {hold_min:.0f} min "
                            f"(mín {int(MIN_HOLD_MIN)} min para AI Signal)")
                        return
                except Exception:
                    pass  # si falla el parse, dejamos pasar el SELL

                # ── Debounce: requiere 2 ciclos consecutivos de SELL antes de ejecutar ─
                # Evita salidas prematuras por una sola vela negativa
                self._consecutive_sell[pair.symbol] = self._consecutive_sell.get(pair.symbol, 0) + 1
                consecutive = self._consecutive_sell[pair.symbol]
                if consecutive < 3:
                    self._log("INFO",
                        f"{pair.symbol}: SELL señal {consecutive}/3 — esperando confirmación "
                        f"(AI score: {result.ai_score:+.3f})")
                    return
                # 3+ consecutive SELL signals → execute
                self._consecutive_sell[pair.symbol] = 0
                # SELLs are always automatic — waiting for approval risks capital in fast markets
                self._log("INFO", f"{pair.symbol}: AUTO SELL confirmado (AI score: {result.ai_score:+.3f})")
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
            "fear_greed": self.strategy.get_fear_greed(),
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
