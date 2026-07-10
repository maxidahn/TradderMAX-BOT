"""
Celerity v2 — Configuración
============================
Todo se controla por variables de entorno (.env). NO pongas claves en el código.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List

# Carga .env si existe (no rompe en Railway, donde las vars vienen del entorno)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


@dataclass
class ConfigV2:
    # ── Binance (Futures USDT-M) ─────────────────────────────────────────────
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    testnet: bool = _flag("BINANCE_TESTNET", "false")

    # ── Modo ─────────────────────────────────────────────────────────────────
    # paper_trade=True simula órdenes (NO toca tu dinero). Arrancá siempre acá.
    paper_trade: bool = _flag("V2_PAPER", "true")
    paper_starting_equity: float = float(os.getenv("V2_PAPER_EQUITY", "500"))

    # ── Seguridad del webhook ────────────────────────────────────────────────
    # Debe coincidir EXACTO con el "secret" del Pine Script.
    webhook_secret: str = os.getenv("V2_WEBHOOK_SECRET", "CAMBIA_ESTE_SECRETO")

    # ── Gestión de riesgo ────────────────────────────────────────────────────
    risk_pct: float = float(os.getenv("V2_RISK_PCT", "0.01"))      # 1% del equity por trade
    leverage: int = int(os.getenv("V2_LEVERAGE", "1"))            # 1x (conservador)
    max_open_positions: int = int(os.getenv("V2_MAX_POSITIONS", "2"))
    max_daily_loss_pct: float = float(os.getenv("V2_MAX_DAILY_LOSS_PCT", "4"))  # kill switch
    min_notional: float = float(os.getenv("V2_MIN_NOTIONAL", "20"))            # mínimo Binance Futures
    max_notional: float = float(os.getenv("V2_MAX_NOTIONAL", "150"))

    # ── Pares permitidos (whitelist — el webhook rechaza cualquier otro) ──────
    allowed_symbols: List[str] = field(default_factory=lambda: [
        s.strip().upper() for s in os.getenv("V2_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    ])

    # ── Telegram (opcional) ──────────────────────────────────────────────────
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Servidor ─────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = int(os.getenv("PORT", "8080"))
    data_dir: str = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def status_line(self) -> str:
        mode = "PAPER" if self.paper_trade else "LIVE"
        creds = "con claves" if self.has_credentials else "SIN claves"
        return (f"Celerity v2 [{mode}] {creds} | pares={self.allowed_symbols} | "
                f"riesgo={self.risk_pct:.0%}/trade | lev={self.leverage}x | "
                f"kill-switch={self.max_daily_loss_pct}%")


config = ConfigV2()
