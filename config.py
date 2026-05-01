"""
Celerity Trader Bot - Configuration
====================================
Trading bot for BTC/USDT and PAXG/USDT on Binance.
Operates only during NYSE market hours (9:30 AM - 4:00 PM ET).
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BinanceConfig:
    """Binance API configuration."""
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    testnet: bool = os.getenv("BINANCE_TESTNET", "true").lower() in ("true", "1", "yes")

    @property
    def has_credentials(self) -> bool:
        """Check if API credentials are configured."""
        return bool(self.api_key and self.api_secret)

    @property
    def credentials_status(self) -> str:
        """Human-readable status of API credentials."""
        if not self.api_key and not self.api_secret:
            return "NO_KEYS: Neither BINANCE_API_KEY nor BINANCE_API_SECRET are set"
        if not self.api_key:
            return "MISSING: BINANCE_API_KEY is not set"
        if not self.api_secret:
            return "MISSING: BINANCE_API_SECRET is not set"
        key_preview = self.api_key[:6] + "..." + self.api_key[-4:] if len(self.api_key) > 10 else "***"
        return f"OK: Key configured ({key_preview}), testnet={self.testnet}"


@dataclass
class TradingPair:
    """Configuration for a single trading pair."""
    symbol: str
    name: str
    trade_amount_usdt: float = 3.0  # Default $3 per trade
    min_trade_usdt: float = 1.0
    max_trade_usdt: float = 5.0
    enabled: bool = True


@dataclass
class TransactionCosts:
    """Transaction cost parameters for realistic P&L calculation."""
    # Binance fees (maker/taker)
    fee_rate: float = 0.001        # 0.1% per trade (default Binance spot)
    fee_rate_bnb: float = 0.00075  # 0.075% if paying fees with BNB
    use_bnb_fee: bool = False      # Whether user pays fees in BNB

    # Slippage estimation
    slippage_base_pct: float = 0.05   # 0.05% base slippage for market orders
    slippage_volatile_pct: float = 0.15  # 0.15% slippage in volatile regime
    slippage_quiet_pct: float = 0.03     # 0.03% slippage in quiet regime

    # Spread thresholds
    max_spread_pct: float = 0.3    # Reject trade if spread > 0.3%
    spread_warning_pct: float = 0.15  # Log warning if spread > 0.15%

    @property
    def effective_fee_rate(self) -> float:
        """Fee rate actually applied per trade."""
        return self.fee_rate_bnb if self.use_bnb_fee else self.fee_rate

    @property
    def round_trip_fee_pct(self) -> float:
        """Total fee % for a complete buy+sell cycle."""
        return self.effective_fee_rate * 2 * 100  # As percentage


@dataclass
class StrategyConfig:
    """Technical analysis strategy parameters."""
    # EMA Crossover
    ema_fast: int = 9
    ema_slow: int = 21

    # RSI
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Volume confirmation
    volume_ma_period: int = 20
    volume_threshold: float = 1.2  # Volume must be 1.2x average

    # Risk management
    stop_loss_pct: float = 2.0    # 2% stop loss
    take_profit_pct: float = 3.0  # 3% take profit
    max_open_positions: int = 2   # Max simultaneous positions per pair

    # Candle timeframe
    timeframe: str = "5m"  # 5-minute candles
    lookback_candles: int = 100


@dataclass
class TradingViewConfig:
    """TradingView webhook integration settings."""
    enabled: bool = True
    # Secret token to validate incoming webhooks (prevents unauthorized signals)
    webhook_secret: str = os.getenv("TV_WEBHOOK_SECRET", "celerity_tv_2024")
    # How long a TradingView signal stays valid before expiring (seconds)
    signal_ttl_seconds: int = 300  # 5 minutes
    # Maximum signals to keep in memory per symbol
    max_signals_per_symbol: int = 20
    # Weight of TradingView layer in the strategy (0.0 to disable, up to 0.20)
    strategy_weight: float = 0.15
    # Minimum confidence from TV signal to be considered (0.0 - 1.0)
    min_confidence: float = 0.3


@dataclass
class ScheduleConfig:
    """NYSE market hours schedule."""
    market_open_hour: int = 9
    market_open_minute: int = 30
    market_close_hour: int = 16
    market_close_minute: int = 0
    timezone: str = "US/Eastern"
    # Days: 0=Monday ... 4=Friday
    trading_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])


@dataclass
class AppConfig:
    """Main application configuration."""
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: TransactionCosts = field(default_factory=TransactionCosts)
    tradingview: TradingViewConfig = field(default_factory=TradingViewConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    trading_pairs: List[TradingPair] = field(default_factory=lambda: [
        TradingPair(symbol="BTCUSDC",  name="Bitcoin",       trade_amount_usdt=25.0, min_trade_usdt=15.0, max_trade_usdt=50.0),
        TradingPair(symbol="PAXGUSDT", name="Gold (PAXG)",   trade_amount_usdt=25.0, min_trade_usdt=15.0, max_trade_usdt=50.0, enabled=True),
        TradingPair(symbol="ETHUSDC",  name="Ethereum",      trade_amount_usdt=25.0, min_trade_usdt=15.0, max_trade_usdt=50.0),
        TradingPair(symbol="SOLUSDC",  name="Solana",        trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=40.0),
        TradingPair(symbol="BNBUSDC",  name="BNB",           trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=40.0),
    ])

    # Set to False to trade 24/7 (ignore NYSE market hours)
    restrict_to_market_hours: bool = False

    # Web dashboard
    web_host: str = "0.0.0.0"
    web_port: int = 5001

    # Logging — on Railway logs go inside the persistent DATA_DIR volume
    log_file: str = os.path.join(os.getenv("DATA_DIR", "logs"), "celerity_bot.log")
    log_level: str = "INFO"

    # Bot control
    check_interval_seconds: int = 60  # How often to check for signals (candles are 5m, 60s is enough)

    # ─── Risk Level (1 = very conservative, 5 = default, 10 = aggressive) ───
    risk_level: int = 5  # ⭐ DEFAULT RECOMENDADO: 5

    def get_risk_params(self) -> dict:
        """
        Returns trading parameters scaled to the current risk level.
        risk_level 1 = ultra conservative, 5 = balanced (default), 10 = aggressive.
        """
        r = max(1, min(10, self.risk_level))
        return {
            # Signal threshold: level 1 needs strong signal, level 10 acts on weak signals
            "signal_threshold":      round(0.45 - (r - 1) * 0.035, 3),  # 0.45 → 0.10
            # Stop loss: tighter at low risk, wider at high risk
            "stop_loss_pct":         round(1.0 + (r - 1) * 0.33, 2),    # 1.0% → 4.0%
            # Take profit: proportional to stop loss
            "take_profit_pct":       round(1.5 + (r - 1) * 0.50, 2),    # 1.5% → 6.0%
            # Position size multiplier
            "position_size_mult":    round(0.50 + (r - 1) * 0.111, 2),  # 0.50x → 1.5x
            # Minimum confidence required
            "min_confidence":        round(0.70 - (r - 1) * 0.044, 2),  # 0.70 → 0.30
            # Allow trading in volatile regimes
            "trade_volatile":        r >= 4,
            # Allow trading in ranging markets
            "trade_ranging":         r >= 6,
        }


# Global config instance
config = AppConfig()
