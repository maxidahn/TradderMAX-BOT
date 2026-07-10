"""
Celerity v2 — Servidor de Webhooks (TradingView → Binance Futures)
===================================================================
Punto de entrada. Levanta un servidor Flask que:
  1. Recibe el POST de la alerta de TradingView en /webhook/tradingview
  2. Valida el "secret" y el símbolo (whitelist)
  3. Le pasa la orden al ExecutorV2 (paper o live)

Arrancar:
    pip install -r requirements.txt
    cp .env.example .env   &&   editá .env con tus claves
    python app_v2.py

Endpoints:
    GET  /            → healthcheck
    GET  /status      → estado, equity, posiciones, historial
    POST /webhook/tradingview → recibe señales de TradingView
"""
import json
import logging
import os

from flask import Flask, request, jsonify

from config_v2 import config
from executor_v2 import ExecutorV2

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("celerity.v2")


# ── Telegram (opcional) ──────────────────────────────────────────────────────
def make_telegram(cfg):
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return None
    import requests

    def send(msg: str):
        requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": msg}, timeout=8)
    return send


app = Flask(__name__)
telegram = make_telegram(config)
executor = ExecutorV2(config, telegram=telegram)
executor.connect()
logger.info(config.status_line())


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "celerity-v2",
                    "mode": "PAPER" if config.paper_trade else "LIVE"})


@app.get("/status")
def status():
    return jsonify(executor.status())


@app.post("/webhook/tradingview")
def webhook():
    # TradingView manda el body como texto/JSON. Aceptamos ambos.
    raw = request.get_data(as_text=True) or ""
    try:
        data = request.get_json(force=True, silent=True) or json.loads(raw)
    except Exception:
        logger.warning("Webhook con body no-JSON: %s", raw[:200])
        return jsonify({"ok": False, "error": "body no es JSON"}), 400

    # 1. Validar secreto
    if data.get("secret") != config.webhook_secret:
        logger.warning("Webhook RECHAZADO: secreto inválido (ip=%s)", request.remote_addr)
        return jsonify({"ok": False, "error": "secreto inválido"}), 403

    action = str(data.get("action", "")).upper()
    symbol = str(data.get("symbol", "")).upper().replace(".P", "")  # limpia sufijo perp de TV
    sl_pct = float(data.get("sl_pct", 0) or 0)

    # 2. Validar símbolo (whitelist)
    if symbol not in config.allowed_symbols:
        logger.warning("Webhook RECHAZADO: símbolo %s no permitido", symbol)
        return jsonify({"ok": False, "error": f"símbolo {symbol} no permitido"}), 400

    # 3. Ejecutar
    logger.info("Señal TradingView: %s %s (sl=%.2f%%)", action, symbol, sl_pct)
    result = executor.handle_signal(action, symbol, sl_pct)
    code = 200 if result.get("ok") else 422
    return jsonify(result), code


if __name__ == "__main__":
    app.run(host=config.host, port=config.port)
