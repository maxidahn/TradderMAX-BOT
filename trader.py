"""
Celerity Trader Bot - Binance Trading Module
==============================================
Handles all interaction with the Binance API:
  - Fetching candle/kline data
  - Placing market orders (buy/sell)
  - Managing positions
  - Account balance queries
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field

import pandas as pd

import persistence

logger = logging.getLogger("celerity.trader")


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    side: str  # 'BUY'
    entry_price: float
    quantity: float
    usdt_amount: float
    entry_time: str
    order_id: str = ""
    entry_fee: float = 0.0       # Fee paid on entry
    peak_price: float = 0.0     # Highest price seen since entry (trailing stop anchor)
    partial_tp_taken: bool = False  # True once the 50% partial exit has fired


@dataclass
class TradeRecord:
    """Record of a completed trade."""
    symbol: str
    side: str
    price: float
    quantity: float
    usdt_amount: float
    pnl: float       # Net P&L (after fees)
    pnl_pct: float   # Net P&L % (after fees)
    pnl_gross: float = 0.0  # Gross P&L (before fees)
    total_fees: float = 0.0  # Total fees (entry + exit)
    slippage_cost: float = 0.0  # Estimated slippage cost
    reason: str = ""
    timestamp: str = ""
    order_id: str = ""


@dataclass
class SpreadInfo:
    """Current bid/ask spread for a symbol."""
    bid: float
    ask: float
    spread: float       # Absolute spread (ask - bid)
    spread_pct: float   # Spread as % of mid price
    mid_price: float
    timestamp: str = ""


class BinanceTrader:
    """Handles Binance API operations."""

    def __init__(self, binance_config, costs_config=None):
        self.config = binance_config
        self.costs = costs_config
        self.client = None
        self.connected = False

        # ─── Load persisted state from previous session ───
        self.trade_history: List[TradeRecord] = self._load_trade_history()
        self.positions: Dict[str, Position] = self._load_positions()

    def _load_trade_history(self) -> List[TradeRecord]:
        """Restore trade history from disk."""
        raw = persistence.load_trade_history()
        trades = []
        for t in raw:
            try:
                trades.append(TradeRecord(
                    symbol=t["symbol"], side=t["side"], price=t["price"],
                    quantity=t["quantity"], usdt_amount=t["usdt_amount"],
                    pnl=t["pnl"], pnl_pct=t["pnl_pct"],
                    pnl_gross=t.get("pnl_gross", 0),
                    total_fees=t.get("total_fees", 0),
                    slippage_cost=t.get("slippage_cost", 0),
                    reason=t.get("reason", ""), timestamp=t.get("timestamp", ""),
                    order_id=t.get("order_id", ""),
                ))
            except Exception as e:
                logger.warning(f"Could not restore trade record: {e}")
        if trades:
            logger.info(f"Restored {len(trades)} trades from previous session")
        return trades

    def _load_positions(self) -> Dict[str, Position]:
        """Restore open positions from disk."""
        raw = persistence.load_open_positions()
        positions = {}
        for sym, p in raw.items():
            try:
                # peak_price defaults to entry_price if missing (old save files)
                peak = p.get("peak_price", 0.0)
                if peak == 0.0:
                    peak = p["entry_price"]
                positions[sym] = Position(
                    symbol=p["symbol"], side=p["side"],
                    entry_price=p["entry_price"], quantity=p["quantity"],
                    usdt_amount=p["usdt_amount"], entry_time=p["entry_time"],
                    order_id=p.get("order_id", ""), entry_fee=p.get("entry_fee", 0),
                    peak_price=peak,
                )
            except Exception as e:
                logger.warning(f"Could not restore position {sym}: {e}")
        if positions:
            logger.info(f"Restored {len(positions)} open positions from previous session: {list(positions.keys())}")
        return positions

    def connect(self) -> bool:
        """Connect to Binance API."""
        # ─── Pre-flight checks ───
        if not self.config.has_credentials:
            logger.error(f"Cannot connect: {self.config.credentials_status}")
            logger.error("Set environment variables: export BINANCE_API_KEY='...' && export BINANCE_API_SECRET='...'")
            return False

        logger.info(f"Credentials check: {self.config.credentials_status}")
        logger.info(f"Testnet mode: {self.config.testnet}")

        try:
            from binance.client import Client

            if self.config.testnet:
                self.client = Client(
                    self.config.api_key,
                    self.config.api_secret,
                    testnet=True,
                )
                logger.info("Connected to Binance TESTNET")
            else:
                self.client = Client(
                    self.config.api_key,
                    self.config.api_secret,
                )
                logger.info("Connected to Binance LIVE")

            # Test connection
            server_time = self.client.get_server_time()
            logger.info(f"Binance server time: {server_time}")
            self.connected = True

            # Immediately test balance access
            try:
                account = self.client.get_account()
                balances = [b for b in account.get("balances", []) if float(b["free"]) > 0 or float(b["locked"]) > 0]
                logger.info(f"Account access OK — found {len(balances)} assets with balance")
                for b in balances:
                    logger.info(f"  {b['asset']}: free={b['free']}, locked={b['locked']}")
            except Exception as e:
                logger.error(f"Connected but CANNOT read account/balance: {e}")
                logger.error("Check API key permissions: 'Enable Reading' must be ON in Binance API settings")

            return True

        except ImportError:
            logger.error("python-binance not installed. Run: pip install python-binance")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to Binance: {type(e).__name__}: {e}")
            # Common error diagnosis
            error_str = str(e).lower()
            if "invalid api-key" in error_str or "api-key" in error_str:
                logger.error("DIAGNOSIS: API key is invalid or not recognized by Binance")
                logger.error("If using Testnet keys, set BINANCE_TESTNET=true")
            elif "timestamp" in error_str:
                logger.error("DIAGNOSIS: System clock is out of sync. Run: sudo ntpdate time.nist.gov")
            elif "permission" in error_str or "restricted" in error_str:
                logger.error("DIAGNOSIS: API key lacks required permissions")
            elif "ip" in error_str:
                logger.error("DIAGNOSIS: Your IP is not whitelisted in Binance API settings")
            return False

    def get_candles(self, symbol: str, interval: str = "5m", limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Fetch kline/candle data from Binance.

        Returns DataFrame with columns: timestamp, open, high, low, close, volume
        """
        if not self.client:
            logger.error("Not connected to Binance")
            return None

        try:
            klines = self.client.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit,
            )

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
            logger.error(f"Failed to fetch candles for {symbol}: {e}")
            return None

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol."""
        if not self.client:
            return None
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    def get_spread(self, symbol: str) -> Optional[SpreadInfo]:
        """
        Get current bid/ask spread from Binance order book.
        Uses best bid/ask (top of book) for accurate spread calculation.
        """
        if not self.client:
            return None
        try:
            ticker = self.client.get_orderbook_ticker(symbol=symbol)
            bid = float(ticker["bidPrice"])
            ask = float(ticker["askPrice"])
            mid = (bid + ask) / 2
            spread = ask - bid
            spread_pct = (spread / mid) * 100 if mid > 0 else 0

            return SpreadInfo(
                bid=bid,
                ask=ask,
                spread=spread,
                spread_pct=round(spread_pct, 4),
                mid_price=mid,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.error(f"Failed to get spread for {symbol}: {e}")
            return None

    def estimate_slippage(self, symbol: str, usdt_amount: float, side: str = "BUY") -> float:
        """
        Estimate slippage % based on order size and order book depth.
        For small orders ($1-$5) slippage is minimal, but we still account for it.
        """
        if not self.costs:
            return 0.0

        base_slippage = self.costs.slippage_base_pct

        # Scale slippage slightly with order size (larger orders = more slippage)
        # For $3 orders this barely moves, but scales for future larger amounts
        size_factor = max(1.0, usdt_amount / 10.0)
        estimated = base_slippage * size_factor

        return round(estimated, 4)

    def check_spread_acceptable(self, symbol: str) -> tuple:
        """
        Check if current spread is acceptable for trading.
        Returns (is_acceptable, spread_info, message).
        """
        if not self.costs:
            return True, None, "No cost config — spread check skipped"

        spread_info = self.get_spread(symbol)
        if not spread_info:
            return True, None, "Could not fetch spread — proceeding with caution"

        if spread_info.spread_pct > self.costs.max_spread_pct:
            msg = (f"SPREAD TOO WIDE: {symbol} spread is {spread_info.spread_pct:.3f}% "
                   f"(max allowed: {self.costs.max_spread_pct}%). Trade blocked.")
            logger.warning(msg)
            return False, spread_info, msg

        if spread_info.spread_pct > self.costs.spread_warning_pct:
            msg = (f"Spread warning: {symbol} at {spread_info.spread_pct:.3f}% "
                   f"(threshold: {self.costs.spread_warning_pct}%)")
            logger.warning(msg)
            return True, spread_info, msg

        return True, spread_info, f"Spread OK: {spread_info.spread_pct:.3f}%"

    def calculate_fee(self, usdt_amount: float) -> float:
        """Calculate fee for a single trade in USDT."""
        if not self.costs:
            return 0.0
        return usdt_amount * self.costs.effective_fee_rate

    def _get_quote_asset(self, symbol: str) -> str:
        """Extract the quote asset from a trading pair symbol."""
        for quote in ("USDC", "USDT", "BUSD", "BTC", "ETH", "BNB"):
            if symbol.endswith(quote):
                return quote
        return "USDT"

    def get_balance(self, asset: str = "USDC") -> float:
        """Get available balance for an asset."""
        if not self.client:
            return 0.0
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return float(balance["free"]) if balance else 0.0
        except Exception as e:
            logger.error(f"Failed to get balance for {asset}: {e}")
            return 0.0

    def get_account_info(self) -> Dict:
        """Get account overview."""
        if not self.client:
            msg = "Not connected to Binance"
            if not self.config.has_credentials:
                msg += f" — {self.config.credentials_status}"
            logger.warning(f"get_account_info called but: {msg}")
            return {"error": msg}

        if not self.connected:
            logger.warning("get_account_info: client exists but connected=False")
            return {"error": "Client initialized but connection not verified"}

        try:
            logger.debug("Fetching account info from Binance...")
            account = self.client.get_account()

            all_balances = account.get("balances", [])
            logger.debug(f"Binance returned {len(all_balances)} total balance entries")

            # Assets with actual balance
            balances = {
                b["asset"]: {
                    "free": float(b["free"]),
                    "locked": float(b["locked"]),
                }
                for b in all_balances
                if float(b["free"]) > 0 or float(b["locked"]) > 0
            }

            # Always include key trading assets even if balance is 0
            KEY_ASSETS = ["USDC", "USDT", "BTC", "ETH", "SOL", "BNB", "PAXG", "BUSD", "FDUSD"]
            for asset_name in KEY_ASSETS:
                if asset_name not in balances:
                    # Search in all_balances for this asset
                    for b in all_balances:
                        if b["asset"] == asset_name:
                            balances[asset_name] = {
                                "free": float(b["free"]),
                                "locked": float(b["locked"]),
                            }
                            break

            # Count non-zero assets for logging
            non_zero = {k: v for k, v in balances.items() if v["free"] > 0 or v["locked"] > 0}
            logger.info(f"Account info OK: {len(non_zero)} assets with balance, {len(balances)} total shown")

            return {
                "balances": balances,
                "status": "OK",
                "account_type": account.get("accountType", "unknown"),
                "can_trade": account.get("canTrade", False),
                "can_withdraw": account.get("canWithdraw", False),
            }

        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"Failed to get account info: {error_type}: {e}")
            return {"error": f"{error_type}: {e}"}

    def get_portfolio_value(self) -> Dict:
        """
        Calculate total portfolio value in USD across all Binance assets.
        Stablecoins (USDC, USDT, BUSD, FDUSD) count at face value.
        Crypto assets are valued at current market price.
        Returns a dict with total, stablecoins, crypto_value, and per-asset breakdown.
        """
        STABLECOINS = {"USDC", "USDT", "BUSD", "FDUSD"}
        if not self.client or not self.connected:
            return {"error": "Not connected", "total": 0.0}

        try:
            account = self.client.get_account()
            all_balances = account.get("balances", [])

            # Filter to assets with non-zero balance
            assets = {
                b["asset"]: float(b["free"]) + float(b["locked"])
                for b in all_balances
                if float(b["free"]) + float(b["locked"]) > 0
            }

            stablecoin_total = 0.0
            crypto_total = 0.0
            breakdown = []

            for asset, amount in assets.items():
                if asset in STABLECOINS:
                    stablecoin_total += amount
                    breakdown.append({
                        "asset": asset,
                        "amount": amount,
                        "price": 1.0,
                        "value_usd": amount,
                        "type": "stablecoin",
                    })
                else:
                    # Try ASSET+USDC first, then ASSET+USDT
                    price = None
                    for quote in ("USDC", "USDT"):
                        try:
                            ticker = self.client.get_symbol_ticker(symbol=f"{asset}{quote}")
                            price = float(ticker["price"])
                            break
                        except Exception:
                            pass
                    if price is None or price == 0.0:
                        # Could not price this asset — skip
                        breakdown.append({
                            "asset": asset,
                            "amount": amount,
                            "price": None,
                            "value_usd": None,
                            "type": "unknown",
                        })
                        continue
                    value = amount * price
                    crypto_total += value
                    breakdown.append({
                        "asset": asset,
                        "amount": amount,
                        "price": price,
                        "value_usd": value,
                        "type": "crypto",
                    })

            # Sort: stablecoins first by value desc, then crypto by value desc
            breakdown.sort(key=lambda x: (x["type"] != "stablecoin", -(x["value_usd"] or 0)))

            total = stablecoin_total + crypto_total
            return {
                "total": round(total, 2),
                "stablecoins": round(stablecoin_total, 2),
                "crypto_value": round(crypto_total, 2),
                "breakdown": breakdown,
            }

        except Exception as e:
            logger.error(f"get_portfolio_value error: {e}")
            return {"error": str(e), "total": 0.0}

    def place_buy(self, symbol: str, usdt_amount: float) -> Optional[Position]:
        """
        Place a market buy order.
        Checks spread before execution and tracks entry fee.

        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            usdt_amount: Amount in USDT to spend

        Returns:
            Position object if successful
        """
        if not self.client:
            logger.error("Not connected")
            return None

        # Check if already in position
        if symbol in self.positions:
            logger.warning(f"Already in position for {symbol}")
            return None

        # ─── Spread check before trading ───
        spread_ok, spread_info, spread_msg = self.check_spread_acceptable(symbol)
        if not spread_ok:
            logger.warning(f"BUY {symbol} BLOCKED: {spread_msg}")
            return None
        if spread_info:
            logger.info(f"BUY {symbol} spread: {spread_info.spread_pct:.3f}% "
                        f"(bid: ${spread_info.bid:.2f}, ask: ${spread_info.ask:.2f})")

        # ─── Balance check before ordering ───
        quote_asset = self._get_quote_asset(symbol)
        available = self.get_balance(quote_asset)
        if available < usdt_amount:
            logger.error(
                f"BUY {symbol} BLOCKED: insufficient {quote_asset} balance "
                f"(need ${usdt_amount:.2f}, have ${available:.2f})"
            )
            return None

        try:
            price = self.get_current_price(symbol)
            if not price:
                return None

            # Calculate entry fee
            entry_fee = self.calculate_fee(usdt_amount)
            slippage_pct = self.estimate_slippage(symbol, usdt_amount, "BUY")

            # Calculate quantity based on quote amount (deduct fee from buying power)
            effective_amount = usdt_amount - entry_fee
            quantity = effective_amount / price

            # Get symbol info for LOT_SIZE precision and MIN_NOTIONAL check
            info = self.client.get_symbol_info(symbol)
            if info:
                for f in info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                        precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
                        quantity = round(quantity - (quantity % step), precision)
                    elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                        min_notional = float(f.get("minNotional", f.get("minQty", 0)))
                        if usdt_amount < min_notional:
                            logger.error(
                                f"BUY {symbol} BLOCKED: amount ${usdt_amount:.2f} below "
                                f"Binance minimum ${min_notional:.2f}"
                            )
                            return None

            if quantity <= 0:
                logger.error(f"Quantity too small for {symbol} with ${usdt_amount} (after fee: ${entry_fee:.4f})")
                return None

            logger.info(f"BUY {symbol}: qty={quantity} @ ~${price} (${usdt_amount}, fee: ${entry_fee:.4f}, est.slip: {slippage_pct}%)")

            order = self.client.order_market_buy(
                symbol=symbol,
                quantity=quantity,
            )

            fills = order.get("fills", [])
            fill_price = float(fills[0].get("price", price)) if fills else price
            fill_qty_gross = float(order.get("executedQty", quantity))

            # ── Deduct base-asset fees from fill quantity ──────────────────────
            # Binance charges spot BUY fees IN the received (base) asset by default.
            # e.g. buy 0.0061 PAXG → Binance keeps 0.0000061 PAXG as fee → you get 0.006094.
            # If we record the gross executedQty we will try to sell more than we own
            # and get APIError(-2010) every time.
            base_asset = symbol
            for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB", "FDUSD"):
                if base_asset.endswith(quote):
                    base_asset = base_asset[:-len(quote)]
                    break
            fee_in_base = sum(
                float(f.get("commission", 0))
                for f in fills
                if f.get("commissionAsset") == base_asset
            )
            fill_qty = fill_qty_gross - fee_in_base
            if fee_in_base > 0:
                logger.info(
                    f"BUY {symbol}: fee deducted from qty — "
                    f"gross: {fill_qty_gross}, fee: {fee_in_base} {base_asset}, net: {fill_qty}"
                )

            # Calculate actual slippage from expected vs fill price
            actual_slippage_pct = ((fill_price - price) / price) * 100 if price > 0 else 0
            if abs(actual_slippage_pct) > slippage_pct * 2:
                logger.warning(f"BUY {symbol}: HIGH SLIPPAGE — expected ~{slippage_pct}%, actual {actual_slippage_pct:+.3f}%")

            position = Position(
                symbol=symbol,
                side="BUY",
                entry_price=fill_price,
                quantity=fill_qty,
                usdt_amount=usdt_amount,
                entry_time=datetime.now(timezone.utc).isoformat(),
                order_id=str(order.get("orderId", "")),
                entry_fee=entry_fee,
                peak_price=fill_price,  # Trailing stop starts from entry price
            )

            self.positions[symbol] = position
            logger.info(f"BUY filled: {symbol} qty={fill_qty} @ ${fill_price} (fee: ${entry_fee:.4f})")
            persistence.save_open_positions(self.positions)
            return position

        except Exception as e:
            logger.error(f"BUY order failed for {symbol}: {e}")
            return None

    def place_partial_sell(self, symbol: str, fraction: float = 0.5) -> Optional[TradeRecord]:
        """
        Sell a fraction of the open position (default 50%) as a partial take profit.
        Updates the position's quantity in-place; does NOT remove the position.
        Marks position.partial_tp_taken = True so this only fires once.
        """
        position = self.positions.get(symbol)
        if not position or position.partial_tp_taken:
            return None

        partial_qty = position.quantity * fraction
        if partial_qty <= 0:
            return None

        # Round to LOT_SIZE precision
        try:
            info = self.client.get_symbol_info(symbol)
            if info:
                for f in info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                        precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
                        partial_qty = round(partial_qty - (partial_qty % step), precision)
        except Exception:
            pass

        if partial_qty <= 0:
            return None

        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None

            order = self.client.order_market_sell(symbol=symbol, quantity=partial_qty)
            fill_price = float(order.get("fills", [{}])[0].get("price", current_price))

            exit_usdt  = fill_price * partial_qty
            exit_fee   = self.calculate_fee(exit_usdt)
            entry_fee_partial = position.entry_fee * fraction
            total_fees = entry_fee_partial + exit_fee

            pnl_gross  = (fill_price - position.entry_price) * partial_qty
            pnl_net    = pnl_gross - total_fees
            pnl_net_pct = (pnl_net / (position.usdt_amount * fraction)) * 100 if position.usdt_amount > 0 else 0

            record = TradeRecord(
                symbol=symbol, side="SELL",
                price=fill_price, quantity=partial_qty,
                usdt_amount=exit_usdt,
                pnl=pnl_net, pnl_pct=pnl_net_pct,
                pnl_gross=pnl_gross, total_fees=total_fees,
                slippage_cost=0.0,
                reason="PARTIAL_TP",
                timestamp=datetime.now(timezone.utc).isoformat(),
                order_id=str(order.get("orderId", "")),
            )

            # Update position: reduce quantity, mark partial taken, adjust entry_fee
            position.quantity     -= partial_qty
            position.entry_fee    -= entry_fee_partial
            position.usdt_amount  *= (1 - fraction)
            position.partial_tp_taken = True

            self.trade_history.append(record)
            self._save_state()

            # ─── ML feedback para PARTIAL_TP ─────────────────────────────────
            # place_partial_sell no llamaba a save_ml_feedback, privando al modelo
            # de aprender de sus mejores salidas (las únicas realmente rentables).
            persistence.save_ml_feedback({
                "symbol":      symbol,
                "entry_price": position.entry_price,
                "exit_price":  fill_price,
                "entry_time":  position.entry_time,
                "exit_time":   record.timestamp,
                "pnl_pct":     round(pnl_net_pct, 4),
                "profitable":  pnl_net > 0,
                "reason":      "PARTIAL_TP",
            })

            logger.info(
                f"PARTIAL_TP {symbol}: sold {fraction*100:.0f}% ({partial_qty}) "
                f"@ ${fill_price:.4f} | pnl: ${pnl_net:+.4f} ({pnl_net_pct:+.2f}%) "
                f"| remaining qty: {position.quantity}"
            )
            return record

        except Exception as e:
            logger.error(f"PARTIAL_TP order failed for {symbol}: {e}")
            return None

    def place_sell(self, symbol: str, reason: str = "Signal") -> Optional[TradeRecord]:
        """
        Close a position by selling.
        Calculates NET P&L after deducting entry + exit fees and slippage.

        Args:
            symbol: Trading pair
            reason: Why selling (Signal, STOP_LOSS, TAKE_PROFIT)

        Returns:
            TradeRecord if successful
        """
        if not self.client:
            logger.error("Not connected")
            return None

        position = self.positions.get(symbol)
        if not position:
            logger.warning(f"No open position for {symbol}")
            return None

        # Skip spread check for ALL exit reasons — closing a position is always
        # more important than spread conditions.  Wide spreads appear precisely
        # when the market is moving against us, so blocking exits then is dangerous.
        # Spread check is only useful for NEW entries (place_buy).
        _SPREAD_BYPASS_EXITS = (
            "STOP_LOSS", "TAKE_PROFIT",
            "Manual close", "AI Signal", "Approved",
            "TRAILING_STOP", "TIMEOUT", "LOSS_TIMEOUT",
        )
        if reason not in _SPREAD_BYPASS_EXITS:
            spread_ok, spread_info, spread_msg = self.check_spread_acceptable(symbol)
            if not spread_ok:
                logger.warning(f"SELL {symbol} BLOCKED: {spread_msg}")
                return None

        try:
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None

            # Calculate exit fee
            exit_usdt = current_price * position.quantity
            exit_fee = self.calculate_fee(exit_usdt)
            entry_fee = position.entry_fee
            total_fees = entry_fee + exit_fee

            # Estimate slippage cost
            slippage_pct = self.estimate_slippage(symbol, exit_usdt, "SELL")
            slippage_cost = exit_usdt * (slippage_pct / 100)

            logger.info(f"SELL {symbol}: qty={position.quantity} @ ~${current_price} ({reason}) "
                        f"[fees: ${total_fees:.4f}, est.slip: ${slippage_cost:.4f}]")

            # ── LOT_SIZE precision: round sell qty to Binance step requirements ──
            sell_qty = position.quantity
            try:
                info = self.client.get_symbol_info(symbol)
                if info:
                    for f in info.get("filters", []):
                        if f["filterType"] == "LOT_SIZE":
                            step = float(f["stepSize"])
                            precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
                            sell_qty = round(sell_qty - (sell_qty % step), precision)
            except Exception as lot_err:
                logger.debug(f"LOT_SIZE check skipped for {symbol}: {lot_err}")

            order = self.client.order_market_sell(
                symbol=symbol,
                quantity=sell_qty,
            )

            fill_price = float(order.get("fills", [{}])[0].get("price", current_price))

            # Gross P&L (before costs)
            pnl_gross = (fill_price - position.entry_price) * position.quantity
            pnl_gross_pct = ((fill_price - position.entry_price) / position.entry_price) * 100

            # Recalculate exit fee with actual fill price
            actual_exit_usdt = fill_price * position.quantity
            exit_fee = self.calculate_fee(actual_exit_usdt)
            total_fees = entry_fee + exit_fee

            # Actual slippage (compare expected vs fill)
            actual_slippage_pct = ((current_price - fill_price) / current_price) * 100 if current_price > 0 else 0
            actual_slippage_cost = actual_exit_usdt * abs(actual_slippage_pct) / 100
            if abs(actual_slippage_pct) > slippage_pct * 2:
                logger.warning(f"SELL {symbol}: HIGH SLIPPAGE — expected ~{slippage_pct}%, actual {actual_slippage_pct:+.3f}%")

            # NET P&L (after fees + slippage)
            pnl_net = pnl_gross - total_fees
            pnl_net_pct = (pnl_net / position.usdt_amount) * 100 if position.usdt_amount > 0 else 0

            record = TradeRecord(
                symbol=symbol,
                side="SELL",
                price=fill_price,
                quantity=position.quantity,
                usdt_amount=actual_exit_usdt,
                pnl=pnl_net,
                pnl_pct=pnl_net_pct,
                pnl_gross=pnl_gross,
                total_fees=total_fees,
                slippage_cost=actual_slippage_cost,
                reason=reason,
                timestamp=datetime.now(timezone.utc).isoformat(),
                order_id=str(order.get("orderId", "")),
            )

            self.trade_history.append(record)
            del self.positions[symbol]

            sign = "+" if pnl_net >= 0 else ""
            logger.info(
                f"SELL filled: {symbol} @ ${fill_price} | "
                f"Gross: {'+' if pnl_gross >= 0 else ''}${pnl_gross:.4f} | "
                f"Fees: -${total_fees:.4f} | "
                f"NET: {sign}${pnl_net:.4f} ({sign}{pnl_net_pct:.2f}%)"
            )
            # ─── Persist after every trade ───
            persistence.save_trade_history(self.trade_history)
            persistence.save_open_positions(self.positions)
            persistence.save_ml_feedback({
                "symbol":            symbol,
                "entry_price":       position.entry_price,
                "exit_price":        fill_price,
                "entry_time":        position.entry_time,
                "exit_time":         datetime.now(timezone.utc).isoformat(),
                "pnl_pct":           round(pnl_net_pct, 4),
                "profitable":        pnl_net > 0,
                "reason":            reason,
            })
            return record

        except Exception as e:
            logger.error(f"SELL order failed for {symbol}: {e}")
            return None

    def get_daily_stats(self) -> Dict:
        """
        Calcula métricas del día actual (UTC).
        Se resetean automáticamente en cada nueva jornada.
        """
        from datetime import date
        today = date.today().isoformat()  # "2026-05-04"
        daily = [t for t in self.trade_history if t.timestamp and t.timestamp[:10] == today]

        if not daily:
            return {
                "trades": 0, "wins": 0, "losses": 0,
                "pnl": 0.0, "pnl_gross": 0.0,
                "win_rate": 0.0, "fees": 0.0, "slippage": 0.0,
                "best_trade": None, "worst_trade": None,
            }

        wins    = [t for t in daily if t.pnl > 0]
        losses  = [t for t in daily if t.pnl <= 0]
        pnl     = sum(t.pnl for t in daily)
        fees    = sum(t.total_fees for t in daily)
        slippage = sum(t.slippage_cost for t in daily)
        win_rate = round(len(wins) / len(daily) * 100, 1)

        best  = max(daily, key=lambda t: t.pnl)
        worst = min(daily, key=lambda t: t.pnl)

        return {
            "trades":    len(daily),
            "wins":      len(wins),
            "losses":    len(losses),
            "pnl":       round(pnl, 4),
            "pnl_gross": round(sum(t.pnl_gross for t in daily), 4),
            "win_rate":  win_rate,
            "fees":      round(fees, 4),
            "slippage":  round(slippage, 4),
            "best_trade":  {"symbol": best.symbol,  "pnl": round(best.pnl, 4),  "pnl_pct": round(best.pnl_pct, 2)},
            "worst_trade": {"symbol": worst.symbol, "pnl": round(worst.pnl, 4), "pnl_pct": round(worst.pnl_pct, 2)},
        }

    def get_status(self) -> Dict:
        """Get trader status for the dashboard (with cost breakdown)."""
        total_pnl_net = sum(t.pnl for t in self.trade_history)
        total_pnl_gross = sum(t.pnl_gross for t in self.trade_history)
        total_fees = sum(t.total_fees for t in self.trade_history)
        total_slippage = sum(t.slippage_cost for t in self.trade_history)
        win_trades = sum(1 for t in self.trade_history if t.pnl > 0)
        total_trades = len(self.trade_history)
        win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

        return {
            "connected": self.connected,
            "testnet": self.config.testnet,
            "open_positions": {
                sym: {
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "usdt_amount": pos.usdt_amount,
                    "entry_time": pos.entry_time,
                    "entry_fee": round(pos.entry_fee, 4),
                }
                for sym, pos in self.positions.items()
            },
            "trade_history": [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "price": t.price,
                    "pnl_net": round(t.pnl, 4),
                    "pnl_gross": round(t.pnl_gross, 4),
                    "pnl_pct": round(t.pnl_pct, 2),
                    "fees": round(t.total_fees, 4),
                    "slippage": round(t.slippage_cost, 4),
                    "reason": t.reason,
                    "timestamp": t.timestamp,
                }
                for t in self.trade_history[-50:]
            ],
            "summary": {
                "total_trades": total_trades,
                "win_rate": round(win_rate, 1),
                "total_pnl_net": round(total_pnl_net, 4),
                "total_pnl_gross": round(total_pnl_gross, 4),
                "total_fees_paid": round(total_fees, 4),
                "total_slippage_cost": round(total_slippage, 4),
                "cost_drag_pct": round((total_fees / total_pnl_gross * 100), 1) if total_pnl_gross > 0 else 0,
                "daily": self.get_daily_stats(),   # ← métricas del día actual
            },
        }
