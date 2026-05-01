"""
Celerity Trader Bot - Telegram Notifier
========================================
Sends trade alerts and bot events to a Telegram chat.

Setup:
  1. Create a bot via @BotFather → get token
  2. Send a message to your bot, then call getUpdates to get your chat_id
  3. Set environment variables:
       export TELEGRAM_TOKEN="7123456789:AAF..."
       export TELEGRAM_CHAT_ID="123456789"

Notifications sent:
  - Bot started / stopped
  - BUY proposed (consultation mode)
  - BUY / SELL executed
  - Stop Loss / Take Profit triggered
  - Critical errors
"""

import logging
import os
import threading
import urllib.request
import urllib.parse
import json
from datetime import datetime

logger = logging.getLogger("celerity.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends Telegram messages asynchronously so they never block the bot loop."""

    def __init__(self):
        self.token    = os.getenv("TELEGRAM_TOKEN", "")
        self.chat_id  = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled  = bool(self.token and self.chat_id)
        self._sent    = 0
        self._errors  = 0

        if self.enabled:
            logger.info("Telegram: notifier enabled ✓")
        else:
            missing = []
            if not self.token:   missing.append("TELEGRAM_TOKEN")
            if not self.chat_id: missing.append("TELEGRAM_CHAT_ID")
            logger.warning(f"Telegram: disabled — missing env vars: {', '.join(missing)}")

    # ─── Public helpers ───────────────────────────────────────────

    def bot_started(self, mode: str, pairs: list):
        pairs_str = ", ".join(pairs)
        self._send(
            f"🤖 *Celerity Bot iniciado*\n"
            f"Modo: `{mode}`\n"
            f"Pares: `{pairs_str}`\n"
            f"⏰ {_now()}"
        )

    def bot_stopped(self):
        self._send(f"⏹ *Celerity Bot detenido* — {_now()}")

    def buy_proposed(self, symbol: str, amount: float, price: float, ai_score: float, reason: str):
        self._send(
            f"🟡 *BUY propuesto — {symbol}*\n"
            f"Precio: `${price:,.2f}` | Monto: `${amount:.2f}`\n"
            f"AI Score: `{ai_score:+.3f}`\n"
            f"📋 {reason[:120]}\n"
            f"➡️ Aprueba en el dashboard"
        )

    def buy_executed(self, symbol: str, price: float, quantity: float, amount: float, ai_score: float):
        self._send(
            f"✅ *COMPRADO — {symbol}*\n"
            f"Precio: `${price:,.2f}` | Qty: `{quantity:.6f}`\n"
            f"Invertido: `${amount:.2f}` | AI: `{ai_score:+.3f}`\n"
            f"⏰ {_now()}"
        )

    def buy_rejected(self, symbol: str):
        self._send(f"❌ *Rechazado* — {symbol} | {_now()}")

    def sell_executed(self, symbol: str, price: float, pnl: float, pnl_pct: float, reason: str):
        icon = "💰" if pnl >= 0 else "📉"
        sign = "+" if pnl >= 0 else ""
        self._send(
            f"{icon} *VENDIDO — {symbol}*\n"
            f"Precio: `${price:,.2f}`\n"
            f"PnL: `{sign}${pnl:.4f}` (`{sign}{pnl_pct:.2f}%`)\n"
            f"Razón: `{reason}`\n"
            f"⏰ {_now()}"
        )

    def stop_loss(self, symbol: str, price: float, pnl: float, pnl_pct: float):
        self._send(
            f"🛑 *STOP LOSS — {symbol}*\n"
            f"Salida: `${price:,.2f}` | PnL: `${pnl:.4f}` (`{pnl_pct:.2f}%`)\n"
            f"⏰ {_now()}"
        )

    def take_profit(self, symbol: str, price: float, pnl: float, pnl_pct: float):
        self._send(
            f"🎯 *TAKE PROFIT — {symbol}*\n"
            f"Salida: `${price:,.2f}` | PnL: `+${pnl:.4f}` (`+{pnl_pct:.2f}%`)\n"
            f"⏰ {_now()}"
        )

    def error(self, symbol: str, message: str):
        self._send(f"⚠️ *Error — {symbol}*\n`{message[:200]}`")

    def risk_changed(self, level: int, label: str, threshold: float, sl: float, tp: float):
        self._send(
            f"⚙️ *Nivel de riesgo → {level}/10*\n"
            f"Perfil: `{label}`\n"
            f"Umbral: `{threshold}` | SL: `{sl}%` | TP: `{tp}%`"
        )

    def get_stats(self) -> dict:
        return {
            "enabled":  self.enabled,
            "sent":     self._sent,
            "errors":   self._errors,
            "chat_id":  self.chat_id[:6] + "..." if self.chat_id else "",
        }

    # ─── Internal ─────────────────────────────────────────────────

    def _send(self, text: str):
        """Send message in a background thread — never blocks the bot loop."""
        if not self.enabled:
            return
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str):
        url = TELEGRAM_API.format(token=self.token)
        payload = json.dumps({
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    self._sent += 1
                else:
                    logger.warning(f"Telegram: HTTP {resp.status}")
                    self._errors += 1
        except Exception as e:
            logger.warning(f"Telegram: send failed — {e}")
            self._errors += 1


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")
