"""
Celerity Trader Bot - Web Dashboard (AI-Enhanced)
===================================================
Flask app with consultation mode endpoints.
"""

import logging
import os
import sys
from flask import Flask, render_template, jsonify, request

# Load .env before anything else so API keys are available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — fall back to env vars already set

from config import config
from bot import CelerityBot
import persistence

os.makedirs("logs", exist_ok=True)

# ─── Restore persisted settings before bot starts ───
config.risk_level = persistence.load_risk_level(default=config.risk_level)

# Restore pair enabled/disabled states
_saved_pairs = persistence.load_pair_states()
for _pair in config.trading_pairs:
    if _pair.symbol in _saved_pairs:
        _pair.enabled = _saved_pairs[_pair.symbol]
logging.basicConfig(
    level=getattr(logging, config.log_level),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(config.log_file),
        logging.StreamHandler(sys.stdout),
    ],
)

app = Flask(__name__)
bot = CelerityBot(config)

# ─── Multi-Agent Futures Orchestrator (optional module) ───
# Solo se inicializa si futures.enabled = True. No afecta al spot bot.
agents_orchestrator = None
if config.futures.enabled:
    try:
        from agents import AgentsOrchestrator
        agents_orchestrator = AgentsOrchestrator(config, telegram=bot.telegram)
        logging.getLogger("celerity").info(
            f"AgentsOrchestrator initialized (paper={config.futures.paper_trade}, "
            f"pairs={[p.symbol for p in config.futures.pairs if p.enabled]})"
        )
    except Exception as e:
        logging.getLogger("celerity").error(f"AgentsOrchestrator init failed: {e}")
        agents_orchestrator = None


@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/status")
def api_status():
    return jsonify(bot.get_full_status())

@app.route("/api/start", methods=["POST"])
def api_start():
    success = bot.start()
    return jsonify({"success": success})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    bot.stop()
    return jsonify({"success": True})

@app.route("/api/pause", methods=["POST"])
def api_pause():
    bot.pause()
    return jsonify({"success": True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    bot.resume()
    return jsonify({"success": True})

@app.route("/api/mode", methods=["POST"])
def api_set_mode():
    data = request.json or {}
    autonomous = data.get("autonomous", False)
    bot.set_autonomous(autonomous)
    return jsonify({"success": True, "autonomous": autonomous})

@app.route("/api/trade/approve", methods=["POST"])
def api_approve():
    data = request.json or {}
    symbol = data.get("symbol")
    success = bot.approve_trade(symbol)
    return jsonify({"success": success})

@app.route("/api/trade/reject", methods=["POST"])
def api_reject():
    data = request.json or {}
    symbol = data.get("symbol")
    success = bot.reject_trade(symbol)
    return jsonify({"success": success})

@app.route("/api/position/close", methods=["POST"])
def api_close_position():
    data = request.json or {}
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"success": False, "error": "symbol required"}), 400
    result = bot.close_position(symbol)
    return jsonify(result)

@app.route("/api/pair/toggle", methods=["POST"])
def api_toggle_pair():
    data = request.json or {}
    symbol = data.get("symbol")
    for pair in config.trading_pairs:
        if pair.symbol == symbol:
            pair.enabled = not pair.enabled
            persistence.save_pair_states(config.trading_pairs)
            return jsonify({"success": True, "enabled": pair.enabled})
    return jsonify({"success": False}), 404

@app.route("/api/portfolio")
def api_portfolio():
    """Return total portfolio value: stablecoins + crypto positions at current prices."""
    return jsonify(bot.trader.get_portfolio_value())


@app.route("/api/balance")
def api_balance():
    """Debug endpoint to test balance directly."""
    info = bot.trader.get_account_info()
    return jsonify({
        "connected": bot.trader.connected,
        "has_credentials": config.binance.has_credentials,
        "credentials_status": config.binance.credentials_status,
        "testnet": config.binance.testnet,
        "client_initialized": bot.trader.client is not None,
        "account": info,
    })

@app.route("/api/readiness")
def api_readiness():
    """
    Full pre-flight check: verifies the bot can actually buy and sell.
    Returns a list of checks with status: ok | warn | error
    """
    checks = []

    def chk(key, label, status, message, detail=""):
        checks.append({"key": key, "label": label, "status": status, "message": message, "detail": detail})

    # 1. Binance connection
    connected = bot.trader.connected and bot.trader.client is not None
    chk("connection", "Conexión Binance",
        "ok" if connected else "error",
        "Conectado" if connected else "No conectado — presiona Start",
        config.binance.credentials_status)

    # 2. API Keys
    chk("api_keys", "API Keys",
        "ok" if config.binance.has_credentials else "error",
        "Configuradas" if config.binance.has_credentials else "Faltan BINANCE_API_KEY / BINANCE_API_SECRET",
        config.binance.credentials_status)

    # 3. Live vs Testnet
    chk("mode", "Modo de operación",
        "warn" if config.binance.testnet else "ok",
        "TESTNET — trades son simulados" if config.binance.testnet else "LIVE — trades reales ✓")

    # 4. USDC balance
    usdc_balance = 0.0
    min_needed = min((p.min_trade_usdt for p in config.trading_pairs if p.enabled), default=10.0)
    if connected:
        usdc_balance = bot.trader.get_balance("USDC")
        if usdc_balance >= min_needed * 2:
            chk("balance", "Balance USDC", "ok",
                f"${usdc_balance:.2f} disponibles", f"Mínimo necesario: ${min_needed:.0f}")
        elif usdc_balance >= min_needed:
            chk("balance", "Balance USDC", "warn",
                f"${usdc_balance:.2f} — suficiente para 1 trade", f"Mínimo: ${min_needed:.0f}")
        else:
            chk("balance", "Balance USDC", "error",
                f"${usdc_balance:.2f} — insuficiente", f"Mínimo para operar: ${min_needed:.0f}")
    else:
        chk("balance", "Balance USDC", "warn", "No verificable sin conexión")

    # 5. Pairs check
    enabled_pairs = [p for p in config.trading_pairs if p.enabled]
    if not enabled_pairs:
        chk("pairs", "Pares activos", "error", "Ningún par habilitado")
    else:
        # Check each pair for USDT vs USDC mismatch
        usdt_pairs = [p for p in enabled_pairs if p.symbol.endswith("USDT") and not p.symbol.endswith("BUSD")]
        usdc_pairs = [p for p in enabled_pairs if p.symbol.endswith("USDC")]
        if usdt_pairs:
            min_usdt_needed = min((p.min_trade_usdt for p in usdt_pairs), default=10.0)
            usdt_balance = bot.trader.get_balance("USDT") if connected else 0.0
            if not connected:
                chk("pairs_usdt", "Pares USDT — balance",
                    "warn",
                    f"{[p.symbol for p in usdt_pairs]} requieren USDT — no verificable sin conexión",
                    "Conéctate para verificar el saldo USDT")
            elif usdt_balance >= min_usdt_needed * 2:
                chk("pairs_usdt", "Pares USDT — balance",
                    "ok",
                    f"Saldo USDT suficiente: ${usdt_balance:.2f} para {[p.symbol for p in usdt_pairs]}",
                    f"Mínimo necesario: ${min_usdt_needed:.0f}")
            elif usdt_balance >= min_usdt_needed:
                chk("pairs_usdt", "Pares USDT — balance",
                    "warn",
                    f"${usdt_balance:.2f} USDT — suficiente para 1 trade en {[p.symbol for p in usdt_pairs]}",
                    f"Mínimo: ${min_usdt_needed:.0f}")
            else:
                chk("pairs_usdt", "Pares USDT — balance",
                    "error",
                    f"${usdt_balance:.2f} USDT — insuficiente para {[p.symbol for p in usdt_pairs]}",
                    f"Mínimo para operar: ${min_usdt_needed:.0f} USDT")
        chk("pairs", "Pares activos",
            "ok" if usdc_pairs else "warn",
            f"{len(enabled_pairs)} pares activos: {[p.symbol for p in enabled_pairs]}")

    # 6. Can trade flag from Binance
    if connected:
        try:
            account = bot.trader.client.get_account()
            can_trade = account.get("canTrade", False)
            chk("can_trade", "Cuenta habilitada para trading",
                "ok" if can_trade else "error",
                "canTrade = True ✓" if can_trade else "canTrade = False — verifica permisos de la API key")
        except Exception as e:
            chk("can_trade", "Permisos de cuenta", "warn", f"No verificable: {e}")

    # 7. Risk level sanity
    risk_params = config.get_risk_params()
    chk("risk", "Nivel de riesgo",
        "ok",
        f"Nivel {config.risk_level}/10 — SL {risk_params['stop_loss_pct']}% / TP {risk_params['take_profit_pct']}% / Umbral {risk_params['signal_threshold']}")

    # 8. Bot running
    chk("bot_running", "Bot corriendo",
        "ok" if bot.running else "warn",
        "Activo — analizando mercado" if bot.running else "Detenido — presiona Start")

    # 9. Open positions
    n_pos = len(bot.trader.positions)
    chk("positions", "Posiciones abiertas",
        "ok",
        f"{n_pos} posición(es) abierta(s)" if n_pos else "Sin posiciones abiertas")

    # 10. Spread config
    chk("spread", "Filtro de spread",
        "ok",
        f"Máximo permitido: {config.costs.max_spread_pct}% — bloquea trades con spread excesivo")

    # 11. Telegram
    tg = bot.telegram
    chk("telegram", "Telegram",
        "ok" if tg.enabled else "warn",
        "Notificaciones activas ✓" if tg.enabled else "Sin configurar (opcional)")

    # 12. Claude Agent
    ca = bot.strategy.claude if hasattr(bot.strategy, 'claude') else None
    claude_ok = ca and ca.enabled if ca else False
    chk("claude", "Claude Agent (Layer 6)",
        "ok" if claude_ok else "warn",
        "Activo ✓" if claude_ok else "Sin ANTHROPIC_API_KEY (opcional)")

    overall = "error" if any(c["status"] == "error" for c in checks) else \
              "warn"  if any(c["status"] == "warn"  for c in checks) else "ok"

    return jsonify({"overall": overall, "checks": checks, "usdc_balance": usdc_balance})


# ─── TradingView Webhook Endpoints ───

@app.route("/api/tv/webhook", methods=["POST"])
def api_tv_webhook():
    """
    Receive TradingView alert webhooks.
    TradingView sends POST with JSON body when an alert fires.
    """
    payload = request.json
    if not payload:
        return jsonify({"status": "error", "message": "Empty payload"}), 400

    result = bot.tv_receiver.validate_and_store(payload)

    if result["status"] == "error":
        return jsonify(result), 401 if "secret" in result.get("message", "") else 400

    return jsonify(result), 200


@app.route("/api/tv/signals")
def api_tv_signals():
    """Get all TradingView signals (active + expired) for dashboard."""
    symbol = request.args.get("symbol", "")
    include_expired = request.args.get("expired", "false").lower() == "true"

    if symbol:
        signals = bot.tv_receiver.get_all_signals(symbol, include_expired)
        consensus = bot.tv_receiver.get_consensus(symbol)
        return jsonify({"symbol": symbol, "signals": signals, "consensus": consensus})

    # All symbols
    all_data = {}
    for pair in config.trading_pairs:
        sym = pair.symbol
        all_data[sym] = {
            "signals": bot.tv_receiver.get_all_signals(sym, include_expired),
            "consensus": bot.tv_receiver.get_consensus(sym),
        }

    return jsonify({"symbols": all_data, "stats": bot.tv_receiver.get_stats()})


@app.route("/api/tv/stats")
def api_tv_stats():
    """Get TradingView webhook stats."""
    return jsonify(bot.tv_receiver.get_stats())


@app.route("/api/tv/test", methods=["POST"])
def api_tv_test():
    """
    Test endpoint — simulate a TradingView signal without needing TV.
    Useful for testing the integration before connecting TradingView.
    """
    payload = request.json or {}
    # Auto-inject the secret for test endpoint
    payload["secret"] = config.tradingview.webhook_secret
    result = bot.tv_receiver.validate_and_store(payload)
    return jsonify(result)


@app.route("/api/risk", methods=["GET", "POST"])
def api_risk():
    if request.method == "POST":
        data = request.json or {}
        level = int(data.get("risk_level", config.risk_level))
        level = max(1, min(10, level))
        config.risk_level = level
        params = config.get_risk_params()
        persistence.save_risk_level(level)
        bot._log("INFO", f"⚙️  Nivel de riesgo cambiado a {level}/10 — threshold: {params['signal_threshold']:.2f}, SL: {params['stop_loss_pct']}%, TP: {params['take_profit_pct']}%")
        return jsonify({"success": True, "risk_level": level, "params": params})
    return jsonify({"risk_level": config.risk_level, "params": config.get_risk_params()})


@app.route("/api/config")
def api_config():
    return jsonify({
        "testnet": config.binance.testnet,
        "strategy": {
            "timeframe": config.strategy.timeframe,
            "stop_loss": config.strategy.stop_loss_pct,
            "take_profit": config.strategy.take_profit_pct,
        },
        "tradingview": {
            "enabled": config.tradingview.enabled,
            "webhook_url": f"http://YOUR_SERVER:{config.web_port}/api/tv/webhook",
            "signal_ttl": config.tradingview.signal_ttl_seconds,
            "strategy_weight": config.tradingview.strategy_weight,
        },
    })


# ─── Tablero de validación (auditoría 2026-06-09) ───

VALIDATION_START_FILE = os.path.join(os.getenv("DATA_DIR", "data"), "validation_start.json")


def _load_validation_start() -> str:
    try:
        import json as _json
        with open(VALIDATION_START_FILE) as f:
            return _json.load(f).get("validation_start", "2026-06-09T19:00:00+00:00")
    except Exception:
        return "2026-06-09T19:00:00+00:00"


def _series_metrics(sells: list) -> dict:
    """Métricas de una serie de trades cerrados (TradeRecord, side=SELL)."""
    n = len(sells)
    if n == 0:
        return {"n": 0}
    wins   = [t.pnl for t in sells if t.pnl > 0]
    losses = [t.pnl for t in sells if t.pnl <= 0]
    pnl_total = sum(t.pnl for t in sells)
    fees      = sum(t.total_fees for t in sells)
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = len(wins) / n * 100
    avg_win  = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    payoff   = (avg_win / avg_loss) if avg_loss > 0 else None
    # Payoff de breakeven dado el win rate: (1-wr)/wr (Hougaard, knowledge/03)
    wr = win_rate / 100
    required_payoff = ((1 - wr) / wr) if wr > 0 else None
    return {
        "n":              n,
        "pnl_total":      round(pnl_total, 4),
        "expectancy":     round(pnl_total / n, 4),
        "win_rate":       round(win_rate, 1),
        "avg_win":        round(avg_win, 4),
        "avg_loss":       round(avg_loss, 4),
        "payoff":         round(payoff, 2) if payoff is not None else None,
        "required_payoff": round(required_payoff, 2) if required_payoff is not None else None,
        "profit_factor":  round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "fees":           round(fees, 4),
        "fees_vs_gross_win_pct": round(fees / gross_win * 100, 1) if gross_win > 0 else None,
    }


@app.route("/api/tablero")
def api_tablero():
    """
    Scorecard de validación del sistema reformado (2026-06-09).
    Mide si el bot está listo para subir riesgo/pasar a real:
    expectativa neta positiva sobre series de ≥25 trades (Douglas),
    payoff suficiente para el win rate (Hougaard), profit factor >1.3,
    regla del 6% mensual (Elder) y estado de aprendizaje de los agentes.
    """
    v_start = _load_validation_start()
    sells_all = [t for t in bot.trader.trade_history if t.side == "SELL"]

    # Serie de validación: trades cerrados desde el deploy del sistema nuevo
    sells_validation = [t for t in sells_all if t.timestamp and t.timestamp >= v_start]
    # Última serie de 25 (contexto histórico)
    sells_last25 = sells_all[-25:]

    m_val   = _series_metrics(sells_validation)
    m_25    = _series_metrics(sells_last25)
    m_total = _series_metrics(sells_all)

    # ── Checklist de objetivos para subir riesgo / pasar a real ──────────────
    TARGET_N = 25
    checks = []
    def _check(label, ok, value, target):
        checks.append({"label": label, "ok": ok, "value": value, "target": target})

    n_val = m_val.get("n", 0)
    enough = n_val >= TARGET_N
    _check("Muestra mínima (serie de validación)",
           True if enough else None, f"{n_val} trades", f"≥ {TARGET_N} trades")
    _check("Expectativa neta por trade > 0",
           (m_val.get("expectancy", 0) > 0) if enough else None,
           f"${m_val.get('expectancy', 0):.3f}" if n_val else "—", "> $0")
    _check("Profit factor ≥ 1.3",
           (m_val.get("profit_factor") or 0) >= 1.3 if enough else None,
           m_val.get("profit_factor") if n_val else "—", "≥ 1.3")
    pf_ok = None
    if enough and m_val.get("payoff") is not None and m_val.get("required_payoff") is not None:
        pf_ok = m_val["payoff"] >= m_val["required_payoff"] * 1.2
    _check("Payoff (gan.media/pérd.media) cubre el win rate +20%",
           pf_ok,
           f"{m_val.get('payoff', '—')} (necesita {m_val.get('required_payoff', '—')})" if n_val else "—",
           "payoff ≥ breakeven × 1.2")
    _check("Fees < 30% de las ganancias brutas",
           (m_val.get("fees_vs_gross_win_pct") or 999) < 30 if enough else None,
           f"{m_val.get('fees_vs_gross_win_pct', '—')}%" if n_val else "—", "< 30%")

    # ── Regla 6% mensual (Elder) ─────────────────────────────────────────────
    monthly = bot.trader.get_monthly_stats()
    capital_base = None
    try:
        if bot.trader.connected:
            capital_base = (
                bot.trader.get_balance("USDC")
                + sum(p.usdt_amount for p in bot.trader.positions.values())
            )
    except Exception:
        pass
    monthly_limit = round(-(6.0 / 100.0) * capital_base, 2) if capital_base else None
    monthly_blocked = (capital_base is not None and monthly["pnl"] <= monthly_limit)

    # ── Agentes (paper) ──────────────────────────────────────────────────────
    agentes = {}
    if agents_orchestrator:
        try:
            stats = agents_orchestrator.replay.stats_per_agent()
            ml = {name: lr.get_state() for name, lr in (agents_orchestrator.online_learners or {}).items()}
            events = agents_orchestrator.tournament.get_recent_events(limit=1)
            agentes = {
                "stats": stats,
                "online_ml": ml,
                "last_tournament": events[-1] if events else None,
                "mode": "PAPER" if config.futures.paper_trade else "LIVE",
            }
        except Exception as e:
            agentes = {"error": str(e)}

    return jsonify({
        "validation_start": v_start,
        "serie_validacion": m_val,
        "serie_25":         m_25,
        "historico":        m_total,
        "checklist":        checks,
        "target_n":         TARGET_N,
        "mes": {
            "month":   monthly["month"],
            "pnl":     monthly["pnl"],
            "trades":  monthly["trades"],
            "capital": round(capital_base, 2) if capital_base else None,
            "limit":   monthly_limit,
            "blocked": monthly_blocked,
        },
        "agentes":    agentes,
        "risk_level": config.risk_level,
    })


# ─── Multi-Agent Futures endpoints (optional module) ───

@app.route("/api/agents/status")
def api_agents_status():
    """Status completo del módulo multi-agente."""
    if not agents_orchestrator:
        return jsonify({"enabled": False, "reason": "agents module disabled or failed to init"}), 200
    return jsonify(agents_orchestrator.get_status())

@app.route("/api/agents/start", methods=["POST"])
def api_agents_start():
    if not agents_orchestrator:
        return jsonify({"success": False, "error": "agents module not available"}), 400
    ok = agents_orchestrator.start()
    return jsonify({"success": ok, "running": agents_orchestrator.running})

@app.route("/api/agents/stop", methods=["POST"])
def api_agents_stop():
    if not agents_orchestrator:
        return jsonify({"success": False}), 400
    agents_orchestrator.stop()
    return jsonify({"success": True})

@app.route("/api/agents/mode", methods=["POST"])
def api_agents_mode():
    """Toggle paper ↔ live. Cuidado: live ejecuta órdenes reales."""
    if not agents_orchestrator:
        return jsonify({"success": False}), 400
    data = request.json or {}
    paper = bool(data.get("paper", True))
    agents_orchestrator.toggle_paper_mode(paper)
    return jsonify({"success": True, "paper_trade": paper})

@app.route("/api/agents/leaderboard")
def api_agents_leaderboard():
    if not agents_orchestrator:
        return jsonify({"leaderboard": []})
    return jsonify({"leaderboard": agents_orchestrator.tournament.get_leaderboard()})

@app.route("/api/agents/tournament/run", methods=["POST"])
def api_tournament_run():
    """Fuerza una corrida del tournament (uso manual / debug)."""
    if not agents_orchestrator:
        return jsonify({"success": False}), 400
    event = agents_orchestrator.tournament.run()
    return jsonify({"success": True, "event": event})

@app.route("/api/agents/position/close", methods=["POST"])
def api_agents_close_position():
    """Cierre manual de una posición de futuros."""
    if not agents_orchestrator:
        return jsonify({"success": False}), 400
    data = request.json or {}
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"success": False, "error": "symbol required"}), 400
    rec = agents_orchestrator.futures_trader.close_position(symbol, reason="MANUAL")
    if rec:
        agents_orchestrator._on_position_closed(rec)
        return jsonify({"success": True, "pnl": rec.pnl_usdt, "pnl_pct": rec.pnl_pct})
    return jsonify({"success": False, "error": "close failed"}), 400

@app.route("/api/agents/replay")
def api_agents_replay():
    """Últimas N entradas del replay buffer (decisiones + outcomes)."""
    if not agents_orchestrator:
        return jsonify({"entries": []})
    n = int(request.args.get("n", 50))
    n = max(1, min(n, 500))
    return jsonify({"entries": agents_orchestrator.replay.recent_closed_all(limit=n)})


# ─── Aprendizaje acelerado: endpoints específicos ───

@app.route("/api/agents/online_ml")
def api_agents_online_ml():
    """Estado de los OnlineLearners por agente (pesos, accuracy, top features)."""
    if not agents_orchestrator or not agents_orchestrator.online_learners:
        return jsonify({"enabled": False, "learners": {}})
    return jsonify({
        "enabled":  True,
        "learners": {name: lr.get_state() for name, lr in agents_orchestrator.online_learners.items()},
    })

@app.route("/api/agents/reflection")
def api_agents_reflection():
    """Últimas reflexiones de Claude post-trade + cambios aplicados."""
    if not agents_orchestrator or not agents_orchestrator.reflector:
        return jsonify({"enabled": False})
    return jsonify(agents_orchestrator.reflector.get_status())

@app.route("/api/agents/contagion")
def api_agents_contagion():
    """Estado del bus de cross-contagion entre agentes."""
    if not agents_orchestrator or not agents_orchestrator.contagion:
        return jsonify({"enabled": False})
    return jsonify(agents_orchestrator.contagion.get_status())

@app.route("/api/agents/bandit")
def api_agents_bandit():
    """Variantes paper actuales por agente + stats."""
    if not agents_orchestrator or not agents_orchestrator.bandit:
        return jsonify({"enabled": False, "variants": {}})
    return jsonify({"enabled": True, "variants": agents_orchestrator.bandit.get_status()})

@app.route("/api/agents/safety_phase")
def api_agents_safety_phase():
    """Estado de la safety phase (leverage 1x durante primeras 72h live)."""
    if not agents_orchestrator:
        return jsonify({"enabled": False})
    safety = {
        "active":        agents_orchestrator.safety_phase_active(),
        "started_at":    agents_orchestrator._live_started_at,
        "hours_total":   agents_orchestrator.fc.safety_phase_hours,
        "max_leverage":  agents_orchestrator.fc.safety_phase_max_leverage,
    }
    if agents_orchestrator._live_started_at:
        import time
        elapsed_h = (time.time() - agents_orchestrator._live_started_at) / 3600.0
        safety["elapsed_hours"] = round(elapsed_h, 1)
        safety["remaining_hours"] = round(max(0, agents_orchestrator.fc.safety_phase_hours - elapsed_h), 1)
    return jsonify(safety)


@app.route("/health")
def health():
    """Railway health check endpoint."""
    return jsonify({"status": "ok", "running": bot.running}), 200


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  CELERITY TRADER BOT — AI Enhanced")
    print("=" * 50)
    print(f"  Mode:     {'TESTNET' if config.binance.testnet else '*** LIVE ***'}")
    print(f"  AI:       Sentiment + ML + Adaptive + TradingView")
    tv_status = "ON" if config.tradingview.enabled else "OFF"
    print(f"  TV Hook:  {tv_status} (weight: {config.tradingview.strategy_weight:.0%})")
    print(f"  TV URL:   http://localhost:{config.web_port}/api/tv/webhook")
    print(f"  Pairs:    {', '.join(p.symbol for p in config.trading_pairs)}")
    print(f"  Hours:    NYSE 9:30-16:00 ET")
    print(f"  Dashboard: http://localhost:{config.web_port}")
    print("=" * 50 + "\n")

    # Connect to Binance immediately so balance shows on dashboard
    print(f"\n  API Key Status: {config.binance.credentials_status}")
    print(f"  Testnet Mode:   {config.binance.testnet}")

    if config.binance.has_credentials:
        print("  Connecting to Binance...")
        if bot.trader.connect():
            print("  Connected! Balance will show on dashboard.")
            # Show balances at startup
            info = bot.trader.get_account_info()
            if "balances" in info:
                print(f"  Found {len(info['balances'])} assets with balance:")
                for asset, bal in list(info["balances"].items())[:5]:
                    total = bal["free"] + bal["locked"]
                    print(f"    {asset}: {total}")
            elif "error" in info:
                print(f"  Balance ERROR: {info['error']}")
        else:
            print("  WARNING: Could not connect. Check logs for details.")
    else:
        print("  WARNING: No API keys configured!")
        print("  Set environment variables:")
        print("    export BINANCE_API_KEY='your_api_key_here'")
        print("    export BINANCE_API_SECRET='your_api_secret_here'")
        print("  For testnet: export BINANCE_TESTNET=true")

    # ─── Auto-start multi-agent module (paper-trade by default) ───
    if agents_orchestrator and config.futures.enabled:
        print()
        mode = "PAPER" if config.futures.paper_trade else "*** LIVE FUTURES ***"
        pairs = ", ".join(p.symbol for p in config.futures.pairs if p.enabled)
        print(f"  Agents Module: {mode}")
        print(f"  Pairs (futures): {pairs}")
        print(f"  Max leverage cap: {config.futures.max_leverage}x")
        print(f"  Starting AgentsOrchestrator...")
        if agents_orchestrator.start():
            print(f"  AgentsOrchestrator running. View at http://localhost:{config.web_port}/#agents")
        else:
            print(f"  WARNING: AgentsOrchestrator did NOT start. Check logs.")
    print()

    # Railway injects PORT; fall back to config value locally
    port = int(os.environ.get("PORT", config.web_port))
    app.run(host=config.web_host, port=port, debug=False)
