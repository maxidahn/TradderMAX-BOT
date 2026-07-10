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
    # Activable por env: USE_BNB_FEE=true (requiere saldo BNB + opción
    # "Pagar fees con BNB" activada en Binance → fee 0.1% → 0.075%, -25%)
    use_bnb_fee: bool = os.getenv("USE_BNB_FEE", "false").lower() in ("true", "1", "yes")

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
    volume_threshold: float = 0.9  # Volume must be 0.9x average (relaxed)

    # Risk management
    stop_loss_pct: float = 2.0    # 2% stop loss
    take_profit_pct: float = 3.0  # 3% take profit
    max_open_positions: int = 4   # Max simultaneous positions (máx 4 de 9 pares activos)

    # Partial Take Profit — sell 50% of position at TP1, let rest run with trailing stop.
    # TP1 = take_profit_pct / 2  (e.g. 6.25% TP → close half at 3.125%)
    # Set to False to disable and use original full-exit TP behavior.
    partial_tp_enabled: bool = True

    # Trailing stop — locks in profits as price rises
    # Set to 0.0 to disable.  Example: 1.5 = close if price drops 1.5% from peak.
    trailing_stop_pct: float = 2.0

    # Position timeout — close any open position after N hours regardless of signal.
    # Prevents capital being locked in stale trades.  Set to 0.0 to disable.
    max_hold_hours: float = 24.0

    # Candle timeframe
    timeframe: str = "5m"  # 5-minute candles
    lookback_candles: int = 150  # Aumentado: da al ML suficientes muestras para entrenar

    # ── Filtro anti-chop (ADX) ───────────────────────────────────────────────
    # Solo permite ENTRADAS (BUY) cuando hay tendencia real. ADX < adx_min = mercado
    # lateral/chop → no operar. Diagnóstico jun-2026: las 21 pérdidas por stop-loss
    # eran entradas en mercado sin dirección que revertían de inmediato.
    # Solo degrada BUY→HOLD; nunca bloquea salidas.
    anti_chop_enabled: bool = True
    adx_period: int = 14
    adx_min: float = 20.0          # ADX mínimo para permitir una entrada

    # ── Gate de coste / volatilidad ──────────────────────────────────────────
    # No entrar si la volatilidad reciente (ATR%) es tan baja que el movimiento
    # esperado no supera claramente el coste ida-vuelta (fees + slippage).
    # Convierte el "micro-trading" en algo seguro: solo se opera cuando el edge
    # esperado paga las comisiones. Solo degrada BUY→HOLD.
    cost_gate_enabled: bool = True
    atr_period: int = 14
    # El ATR% debe ser ≥ cost_gate_atr_mult × coste_ida_vuelta% para entrar.
    cost_gate_atr_mult: float = 1.5


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


# ─────────────────────────────────────────────────────────────────────────────
#  Futures + Multi-Agent module (additive, does not affect spot bot)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FuturesPair:
    """A perpetual pair traded by the agents module."""
    symbol: str            # Binance Futures symbol, e.g. 'SOLUSDT'
    name: str
    leverage: int = 1      # 1x by default (conservative). Capped by FuturesConfig.max_leverage.
    notional_usdt: float = 50.0   # Initial notional per trade
    min_notional: float = 20.0
    max_notional: float = 200.0
    enabled: bool = True


@dataclass
class FuturesConfig:
    """Binance Futures USD-M configuration (perpetuals)."""
    # Master switch — when False, the agents module is fully bypassed.
    enabled: bool = os.getenv("AGENTS_ENABLED", "true").lower() in ("true", "1", "yes")

    # Paper-trade mode — orders are simulated locally, NO real orders sent.
    # Default True so the user explicitly opts into live trading from the dashboard.
    paper_trade: bool = os.getenv("AGENTS_PAPER", "true").lower() in ("true", "1", "yes")

    # Hard caps (apply both to paper and live)
    max_leverage: int = 2                       # Absolute leverage ceiling (was 3 → 2 for safety)
    max_open_positions: int = 4                 # Simultaneous positions across both agents
    max_daily_loss_pct: float = 5.0             # Pct of equity → kill switch + 24h pause
    min_funding_rate_long: float = -0.05        # Block longs if funding > +0.05% (already too crowded)
    max_funding_rate_long: float = 0.05         # %
    funding_guard_enabled: bool = True

    # ── Higher-timeframe trend filter (2026-06-22) ───────────────────────────
    # Evita operar contra la tendencia de fondo: bloquea SHORT cuando la
    # tendencia 1h es alcista y LONG cuando es bajista. Fue la causa #1 de
    # pérdidas en la racha 19-22 jun (shortear el rally de SOL +9%).
    # Validado sobre 113 trades reales: PnL pasa de -0.68 a +0.6/+1.3 en TODAS
    # las combinaciones de lookback/umbral probadas (robusto, no sobreajustado).
    # La tendencia se mide por la PENDIENTE de la EMA50 en 1h, no por la
    # posición del precio (esa versión no servía: los shorts entraban en
    # pullbacks justo debajo de la EMA50 de 5m mientras el fondo seguía alcista).
    htf_trend_filter: bool = True               # Master switch (poné False para revertir)
    htf_interval: str = "1h"                    # Temporalidad de la tendencia de fondo
    htf_ema_period: int = 50                    # EMA de referencia sobre velas 1h
    htf_slope_lookback: int = 6                 # Barras 1h para medir la pendiente (~6h)
    htf_slope_min_pct: float = 0.10             # % mínimo de pendiente para declarar tendencia
    htf_refresh_seconds: int = 900              # Cachea las velas 1h (refresca cada 15 min)

    # ── Live capital configuration (when paper_trade=False) ──────────────────
    # Si todavía no transferiste USDT al wallet Futures, esto solo se usa para
    # validación de tamaños mínimos. La equity real viene de Binance.
    live_starting_capital: float = 100.0        # USDT esperados en wallet Futures

    # ── Safety phase: los primeros N horas tras arrancar live, fuerza 1x ────
    # Permite al sistema acumular 30-50 trades reales antes de subir leverage.
    # Después del period, respeta el cap normal (max_leverage).
    safety_phase_hours: int = 72                # 3 días forzado a 1x
    safety_phase_max_leverage: int = 1          # Leverage durante safety phase

    # Paper-trade starting equity (used to size positions & compute PnL)
    paper_starting_equity: float = 100.0        # USDT virtual balance (matches live)

    # Loop cadence
    tick_seconds: int = 30                      # Agents evaluate every 30s
    sl_tp_seconds: int = 10                     # Fast SL/TP check on open positions

    # Pairs traded by the agents module — sizing pensado para $100 de capital
    # Notional típico $15–25 → con 4 posiciones max usa $60–100 del capital
    # FIX 2026-06-09: min_notional subido a 20 — Binance Futures rechaza
    # órdenes < $20 de notional. El sizing por confianza (0.6-1.0×) bajaba
    # el monto debajo del mínimo real → "open_position: notional $14.73 < min $20".
    # El orchestrator clampea a min_notional, así que con 20 siempre pasa.
    #
    # FIX 2026-06-22 (diversificación): hasta hoy el bot operaba SOLO SOL — los
    # 116 trades fueron todos SOLUSDT. BTC/ETH generaban señales pero NUNCA
    # abrían: BTC ("quantity rounds to 0", su lote mínimo 0.001 BTC ≈ $76 supera
    # su propio max_notional) y ETH ("notional < min", el redondeo hacia abajo
    # lo tiraba bajo el mínimo). Se saca BTC (intradeable con cuenta ~$500) y se
    # agregan alts baratas que ya tienen modelo ML entrenado (LINK/AVAX/XRP)
    # para diversificar de verdad. El sizing en futures_trader.open_position
    # ahora redondea HACIA ARRIBA al lote viable (ver fix allí).
    pairs: List[FuturesPair] = field(default_factory=lambda: [
        FuturesPair(symbol="SOLUSDT",  name="Solana",   leverage=1, notional_usdt=25.0, min_notional=20.0, max_notional=40.0),
        FuturesPair(symbol="ETHUSDT",  name="Ethereum", leverage=1, notional_usdt=25.0, min_notional=20.0, max_notional=40.0),
        FuturesPair(symbol="LINKUSDT", name="Chainlink", leverage=1, notional_usdt=25.0, min_notional=20.0, max_notional=40.0),
        FuturesPair(symbol="AVAXUSDT", name="Avalanche", leverage=1, notional_usdt=25.0, min_notional=20.0, max_notional=40.0),
        FuturesPair(symbol="XRPUSDT",  name="XRP",       leverage=1, notional_usdt=25.0, min_notional=20.0, max_notional=40.0),
    ])


@dataclass
class AgentParams:
    """Tunable parameters for a single agent. Subject to tournament crossover/mutation."""
    # Common
    min_confidence: float = 0.55      # Required confidence to fire a trade
    sl_pct: float = 1.5               # Stop loss % (of entry)
    tp_pct: float = 3.0               # Take profit % (of entry)
    max_hold_minutes: int = 240       # Max hold time before forced exit
    trailing_after_pct: float = 1.0   # Activate trailing stop once gain ≥ this %
    trailing_distance_pct: float = 0.8

    # Momentum agent specific
    ema_fast: int = 9
    ema_slow: int = 21
    adx_min: float = 22.0             # Minimum ADX to consider a trend
    volume_min_ratio: float = 1.1     # Volume vs 20-period MA

    # Reversion agent specific
    rsi_extreme_low: float = 25.0     # Long below this RSI
    rsi_extreme_high: float = 75.0    # Short above this RSI
    bb_period: int = 20
    bb_std: float = 2.0
    funding_extreme: float = 0.03     # % — extreme funding triggers contra trade


@dataclass
class AgentsConfig:
    """Configuration for the two-agent learning system.

    FIX 2026-06-09 (auditoría P2): el modo "ULTRA agresivo" (torneo cada 5
    trades, elegibilidad con 3) optimizaba RUIDO — con n=3-8 la "ventaja" de
    un agente sobre otro es azar (Douglas: el edge solo se ve en series de
    ≥25 trades; Steenbarger: no optimizar sobre muestras chicas). Resultado
    real en 5 semanas: 69 defensive_mode, 1 solo crossover. Volvemos a
    cadencia con disciplina estadística.
    """
    # ── Tournament cadence — por series, no por trade ────────────────────────
    tournament_every_n_trades: int = 25        # Evalúa por series de 25 (Douglas)
    tournament_every_hours: int = 24           # Y al menos 1 vez al día

    # Winner threshold: ganador debe superar perdedor por X% en expectativa neta
    tournament_min_edge_pct: float = 20.0

    # Crossover: 70% del ganador
    crossover_winner_weight: float = 0.70

    # Mutation: 10% de ruido (15% exploraba demasiado con tan pocos datos)
    mutation_rate: float = 0.10

    # Min trades each agent must have before tournament considers them
    min_trades_for_tournament: int = 25        # Muestra mínima estadística

    # Adjudicator (Claude) — only fires on contradictory signals
    adjudicator_enabled: bool = True

    # ── Online ML por agente (#2) ────────────────────────────────────────────
    # FIX 2026-06-09: con 8 samples el perceptrón era ANTI-predictivo
    # (accuracy 30-37%, peor que moneda) y metía ruido en la confidence.
    # Ahora exige 30 samples Y accuracy >50% (gate en base_agent) para opinar.
    online_ml_enabled: bool = True
    online_ml_min_samples: int = 30            # Mín trades cerrados antes de usar score
    online_ml_weight: float = 0.25             # Peso del score ML en la confidence final
    online_ml_min_accuracy: float = 0.50       # Accuracy mínima para que el score cuente

    # ── Reflection con Claude tras cada cierre (#3) ──────────────────────────
    reflection_enabled: bool = True
    reflection_model: str = "claude-haiku-4-5-20251001"
    reflection_apply_threshold: int = 3        # 3 cierres consecutivos sugiriendo lo mismo → aplicar
    reflection_max_delta_pct: float = 0.10     # Máximo 10% de cambio por aplicación

    # ── Bandit: variantes paper paralelas (#4) ───────────────────────────────
    bandit_enabled: bool = True
    bandit_variants_per_agent: int = 3         # 3 hijos x 2 padres = 6 variantes totales
    bandit_promotion_min_trades: int = 10      # Min trades sintéticos antes de poder promover
    bandit_promotion_min_edge_pct: float = 25.0  # Edge que necesita la variante para suplantar al padre

    # ── Cross-contagion en tiempo real (#5) ──────────────────────────────────
    contagion_enabled: bool = True
    contagion_boost_winner: float = 0.15       # +confidence si rival ganó en setup similar
    contagion_boost_loser: float = -0.10       # -confidence si rival perdió en setup similar
    contagion_lookback_minutes: int = 60       # Cuán reciente debe ser el evento
    contagion_similarity_threshold: float = 0.75  # Coseno mín para considerar "setup similar"

    # Initial parameters for both agents
    momentum_initial: AgentParams = field(default_factory=lambda: AgentParams(
        ema_fast=9, ema_slow=21, adx_min=22.0, volume_min_ratio=1.1,
        min_confidence=0.55, sl_pct=1.5, tp_pct=3.5, max_hold_minutes=240,
        trailing_after_pct=1.2, trailing_distance_pct=0.8,
    ))
    # ⚡ EXPLORATION BOOST 2026-05-19 (v3 — desbloqueo de setups bloqueados por replay_mult)
    # Diagnóstico: Sniper detecta BB touch low en BTC/ETH/SOL simultáneo pero
    # confidence final 0.24 (= score 0.30 × replay_mult 0.88) no llega al gate 0.40.
    # Bajamos min_confidence a 0.30 para que setups de calidad pasen aun con
    # penalización del replay buffer. OnlineML (cap 8 samples) compensará después.
    # Originales: rsi_extreme_low=25, rsi_extreme_high=75, funding_extreme=0.03, min_confidence=0.50
    reversion_initial: AgentParams = field(default_factory=lambda: AgentParams(
        rsi_extreme_low=30.0, rsi_extreme_high=70.0, bb_period=20, bb_std=2.0,
        funding_extreme=0.015, min_confidence=0.30, sl_pct=1.2, tp_pct=2.4,
        max_hold_minutes=120, trailing_after_pct=0.8, trailing_distance_pct=0.5,
    ))


@dataclass
class AppConfig:
    """Main application configuration."""
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: TransactionCosts = field(default_factory=TransactionCosts)
    tradingview: TradingViewConfig = field(default_factory=TradingViewConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    futures: FuturesConfig = field(default_factory=FuturesConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    trading_pairs: List[TradingPair] = field(default_factory=lambda: [
        # ── Core (high liquidity, crypto-native, spreads < 0.03%) ──────────────
        TradingPair(symbol="BTCUSDC",  name="Bitcoin",       trade_amount_usdt=90.0, min_trade_usdt=85.0, max_trade_usdt=100.0),
        TradingPair(symbol="ETHUSDC",  name="Ethereum",      trade_amount_usdt=25.0, min_trade_usdt=15.0, max_trade_usdt=100.0, enabled=False),  # DESACTIVADO: 12 trades, 1 win, -9.17% acumulado
        TradingPair(symbol="SOLUSDC",  name="Solana",        trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0),
        # ── Diversification (lower BTC correlation) ────────────────────────────
        TradingPair(symbol="LINKUSDC", name="Chainlink",     trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0),
        TradingPair(symbol="AVAXUSDC", name="Avalanche",     trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0),
        # ── Moderate (Binance utility token, decent liquidity) ─────────────────
        TradingPair(symbol="BNBUSDC",  name="BNB",           trade_amount_usdt=15.0, min_trade_usdt=10.0, max_trade_usdt=60.0),
        # ── High volume altcoins (good RSI/EMA response) ───────────────────────
        TradingPair(symbol="XRPUSDC",  name="XRP",           trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0),
        TradingPair(symbol="ADAUSDC",  name="Cardano",       trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0),
        TradingPair(symbol="DOTUSDC",  name="Polkadot",      trade_amount_usdt=20.0, min_trade_usdt=10.0, max_trade_usdt=75.0,  enabled=False),  # DESACTIVADO: 5 trades, 0 wins, -4.85% acumulado
        # ── Disabled — PAXG: gold asset, RSI/EMA not designed for it,
        #    spread ~0.8% wipes the SL margin, USDT quote is inconsistent ───────
        TradingPair(symbol="PAXGUSDT", name="Gold (PAXG)",   trade_amount_usdt=10.0, min_trade_usdt=10.0, max_trade_usdt=20.0, enabled=False),
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
    risk_level: int = 2  # 2026-05-18: bajado de 4 → 2 tras 81 trades / WR 27% / -$14.84 PnL
                         # Mercado castigando longs spot — pasamos a modo ultra-selectivo
                         # hasta que cambie el régimen o el optimizer suba el nivel solo

    def get_risk_params(self) -> dict:
        """
        Returns trading parameters scaled to the current risk level.
        risk_level 1 = ultra conservative, 5 = balanced (default), 10 = aggressive.

        Cambios v2 (dinámica mejorada):
        - TP siempre = 2.5× SL  →  ratio R/R garantizado ≥ 1:2.5 en todos los niveles
        - position_pct_capital   →  sizing como % del balance disponible (crece con el capital)
        - sell_threshold_mult    →  umbral de SELL escalado por nivel (salida más ágil en bajo riesgo)
        """
        r = max(1, min(10, self.risk_level))
        # Stop loss escalado (1.0% nivel 1 → 3.7% nivel 10)
        sl = round(1.0 + (r - 1) * 0.30, 2)
        # Take profit = 2.0× SL → R/R 1:2 — más alcanzable en mercados con baja volatilidad
        tp = round(sl * 2.0, 2)
        return {
            # Umbral de señal: nivel 1 necesita señal fuerte, nivel 10 actúa con señales débiles
            "signal_threshold":      round(0.32 - (r - 1) * 0.022, 3),  # 0.32 → 0.12
            # Stop loss / take profit con R/R garantizado 1:2.5
            "stop_loss_pct":         sl,                                  # 1.0% → 3.7%
            "take_profit_pct":       tp,                                  # 2.5% → 9.25%
            # Multiplicador de tamaño de posición (complementa el % dinámico)
            "position_size_mult":    round(0.50 + (r - 1) * 0.111, 2),  # 0.50x → 1.5x
            # Confianza mínima requerida
            "min_confidence":        round(0.70 - (r - 1) * 0.044, 2),  # 0.70 → 0.30
            # Permitir trading en regímenes volátiles / ranging
            "trade_volatile":        r >= 4,
            "trade_ranging":         r >= 6,
            # ── NUEVO: Sizing dinámico como % del capital disponible ──────────
            # Se ajusta automáticamente conforme crece el balance
            "position_pct_capital":  round(0.03 + (r - 1) * 0.010, 3),  # 3% → 12% del balance
            # ── NUEVO: Umbral de SELL relativo al de BUY ─────────────────────
            # Nivel bajo: sale rápido (0.75×), nivel alto: espera más convicción (1.0×)
            "sell_threshold_mult":   round(0.75 + (r - 1) * 0.028, 2),  # 0.75 → 1.00
        }


# Global config instance
config = AppConfig()
