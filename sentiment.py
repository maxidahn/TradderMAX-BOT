"""
Celerity Trader Bot - Sentiment Analysis Engine
=================================================
Analyzes market sentiment from multiple free sources:
  1. CryptoCompare News API (free, no key needed for basic)
  2. CoinGecko market data (market cap change)
  3. Price momentum as sentiment proxy
  4. Alternative.me Fear & Greed Index (crypto-specific, updates daily)

Produces a sentiment score from -1.0 (extreme fear) to +1.0 (extreme greed).
"""

import logging
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("celerity.sentiment")

# ─── Keyword dictionaries for sentiment scoring ─────────

BULLISH_KEYWORDS = [
    "bullish", "surge", "soar", "rally", "breakout", "moon", "pump",
    "all-time high", "ath", "buy", "accumulate", "adoption", "upgrade",
    "partnership", "institutional", "etf approved", "approval", "growth",
    "support", "bounce", "recovery", "profit", "gain", "green",
    "optimistic", "confident", "strong", "momentum", "uptrend",
    "halving", "scarcity", "demand", "inflow", "safe haven",
]

BEARISH_KEYWORDS = [
    "bearish", "crash", "dump", "plunge", "sell-off", "selloff",
    "fear", "panic", "bubble", "scam", "hack", "ban", "regulation",
    "crackdown", "lawsuit", "sec", "investigation", "fraud", "rug pull",
    "liquidation", "capitulation", "resistance", "breakdown", "loss",
    "red", "pessimistic", "weak", "decline", "downtrend", "outflow",
    "recession", "inflation", "rate hike", "hawkish", "default",
]


@dataclass
class SentimentResult:
    """Result of sentiment analysis."""
    score: float           # -1.0 to +1.0
    label: str             # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    news_score: float      # Score from news analysis
    momentum_score: float  # Score from price momentum
    market_score: float    # Score from market indicators
    fear_greed_score: float  # Score from Fear & Greed Index (-1 to +1)
    fear_greed_value: int    # Raw F&G value (0-100)
    fear_greed_label: str    # e.g. "Fear", "Extreme Greed"
    headlines: List[str]   # Recent relevant headlines
    confidence: float      # 0.0 to 1.0
    timestamp: str


def _score_to_label(score: float) -> str:
    """Convert numeric score to human-readable label."""
    if score <= -0.6:
        return "Extreme Fear"
    elif score <= -0.2:
        return "Fear"
    elif score <= 0.2:
        return "Neutral"
    elif score <= 0.6:
        return "Greed"
    else:
        return "Extreme Greed"


def _analyze_text(text: str) -> float:
    """
    Score a piece of text for bullish/bearish sentiment.
    Returns -1.0 to +1.0.
    """
    text_lower = text.lower()
    bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
    bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)

    total = bull_count + bear_count
    if total == 0:
        return 0.0

    return (bull_count - bear_count) / total


class SentimentAnalyzer:
    """Multi-source sentiment analysis engine."""

    def __init__(self):
        self._cache: Dict[str, dict] = {}
        self._cache_ttl = 300          # 5 min cache for price/news data
        self._fg_cache: dict = {}      # Separate cache for Fear & Greed (updates daily)
        self._fg_cache_ttl = 3600 * 4  # 4 hours — F&G only changes once/day

    def analyze(self, symbol: str) -> SentimentResult:
        """
        Run full sentiment analysis for a symbol.

        Args:
            symbol: 'BTCUSDT' or 'PAXGUSDT'

        Returns:
            SentimentResult with combined score
        """
        # Check cache
        cache_key = symbol
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached["time"] < self._cache_ttl:
                return cached["result"]

        # Determine search terms
        if "BTC" in symbol:
            search_terms = ["bitcoin", "btc", "crypto"]
            asset = "bitcoin"
        elif "PAXG" in symbol:
            search_terms = ["gold", "xau", "precious metals", "paxg"]
            asset = "gold"
        else:
            search_terms = [symbol.lower()]
            asset = symbol.lower()

        # Gather signals from multiple sources
        news_score, headlines           = self._analyze_news(search_terms)
        momentum_score                  = self._analyze_momentum(symbol)
        market_score                    = self._analyze_market_indicators(asset)
        fg_score, fg_value, fg_label    = self._fear_greed_index()

        # ── Weighted combination ──────────────────────────────────────────────
        # Fear & Greed is the most reliable crypto sentiment signal → 35% weight
        # Only apply F&G for crypto assets (not gold)
        if asset != "gold" and fg_value > 0:
            combined = (
                news_score     * 0.20 +
                momentum_score * 0.30 +
                market_score   * 0.15 +
                fg_score       * 0.35
            )
        else:
            combined = (
                news_score     * 0.35 +
                momentum_score * 0.40 +
                market_score   * 0.25
            )

        # Clamp to [-1, 1]
        combined = max(-1.0, min(1.0, combined))

        # Confidence based on how many sources provided data
        sources_active = sum([
            abs(news_score)     > 0.01,
            abs(momentum_score) > 0.01,
            abs(market_score)   > 0.01,
            fg_value            > 0,
        ])
        confidence = sources_active / 4.0

        result = SentimentResult(
            score=round(combined, 3),
            label=_score_to_label(combined),
            news_score=round(news_score, 3),
            momentum_score=round(momentum_score, 3),
            market_score=round(market_score, 3),
            fear_greed_score=round(fg_score, 3),
            fear_greed_value=fg_value,
            fear_greed_label=fg_label,
            headlines=headlines[:5],
            confidence=round(confidence, 2),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Cache it
        self._cache[cache_key] = {"time": time.time(), "result": result}
        return result

    def _analyze_news(self, search_terms: List[str]) -> tuple:
        """
        Fetch and analyze recent news headlines.
        Uses CryptoCompare News API (free tier).
        """
        headlines = []
        scores = []

        try:
            # CryptoCompare free news API
            url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN"
            resp = requests.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                articles = data.get("Data", [])

                for article in articles[:30]:
                    title = article.get("title", "")
                    body = article.get("body", "")[:200]
                    text = f"{title} {body}"

                    # Check if relevant to our asset
                    text_lower = text.lower()
                    if any(term in text_lower for term in search_terms):
                        score = _analyze_text(text)
                        scores.append(score)
                        headlines.append(title)

        except Exception as e:
            logger.debug(f"News fetch failed: {e}")

        if not scores:
            return 0.0, []

        avg_score = sum(scores) / len(scores)
        return avg_score, headlines

    def _analyze_momentum(self, symbol: str) -> float:
        """
        Analyze price momentum as a sentiment proxy.
        Uses Binance public API (no key needed).
        """
        try:
            # Get 24h price change
            url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
            resp = requests.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                pct_change = float(data.get("priceChangePercent", 0))

                # Map % change to sentiment score
                # ±5% = ±0.5 sentiment, ±10% = ±1.0
                score = max(-1.0, min(1.0, pct_change / 10.0))
                return score

        except Exception as e:
            logger.debug(f"Momentum analysis failed: {e}")

        return 0.0

    def _analyze_market_indicators(self, asset: str) -> float:
        """
        Analyze broader market indicators.
        Uses CoinGecko for crypto, public APIs for gold.
        """
        try:
            if asset == "bitcoin":
                return self._crypto_market_score()
            elif asset == "gold":
                return self._gold_market_score()
        except Exception as e:
            logger.debug(f"Market indicator analysis failed: {e}")

        return 0.0

    def _crypto_market_score(self) -> float:
        """Get crypto market sentiment from CoinGecko global data."""
        try:
            url = "https://api.coingecko.com/api/v3/global"
            resp = requests.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json().get("data", {})
                # Market cap change as indicator
                mkt_cap_change = data.get("market_cap_change_percentage_24h_usd", 0)
                score = max(-1.0, min(1.0, mkt_cap_change / 8.0))
                return score

        except Exception:
            pass
        return 0.0

    def _gold_market_score(self) -> float:
        """
        Gold sentiment based on price momentum.
        Gold typically rises in fear environments (inverse sentiment).
        """
        try:
            # Use PAXG price from Binance as gold proxy
            url = "https://api.binance.com/api/v3/ticker/24hr?symbol=PAXGUSDT"
            resp = requests.get(url, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                pct_change = float(data.get("priceChangePercent", 0))
                # Gold rising = positive for gold traders
                score = max(-1.0, min(1.0, pct_change / 5.0))
                return score

        except Exception:
            pass
        return 0.0

    def _fear_greed_index(self) -> tuple:
        """
        Fetches the Crypto Fear & Greed Index from Alternative.me.
        API: https://api.alternative.me/fng/
        - Free, no key required
        - Updates once per day at midnight UTC
        - Value: 0 (Extreme Fear) → 100 (Extreme Greed)

        Returns: (score -1.0..+1.0, raw_value 0-100, label str)
        """
        # Use dedicated cache — F&G only updates daily
        now = time.time()
        if self._fg_cache and now - self._fg_cache.get("time", 0) < self._fg_cache_ttl:
            cached = self._fg_cache
            return cached["score"], cached["value"], cached["label"]

        try:
            url  = "https://api.alternative.me/fng/?limit=1&format=json"
            resp = requests.get(url, timeout=8)

            if resp.status_code == 200:
                data  = resp.json()
                entry = data.get("data", [{}])[0]
                value = int(entry.get("value", 50))
                label = entry.get("value_classification", "Neutral")

                # Map 0-100 → -1.0 to +1.0
                # 50 = neutral (score 0), 0 = extreme fear (-1), 100 = extreme greed (+1)
                score = (value - 50) / 50.0
                score = round(max(-1.0, min(1.0, score)), 3)

                self._fg_cache = {"score": score, "value": value, "label": label, "time": now}
                logger.info(f"Fear & Greed Index: {value}/100 — {label} (score: {score:+.2f})")
                return score, value, label

        except Exception as e:
            logger.debug(f"Fear & Greed fetch failed: {e}")

        # Fallback: neutral
        return 0.0, 0, "Unavailable"

    def get_summary(self, symbol: str) -> Dict:
        """Get sentiment summary for the dashboard."""
        result = self.analyze(symbol)
        return {
            "score":              result.score,
            "label":              result.label,
            "news_score":         result.news_score,
            "momentum_score":     result.momentum_score,
            "market_score":       result.market_score,
            "fear_greed_value":   result.fear_greed_value,
            "fear_greed_label":   result.fear_greed_label,
            "fear_greed_score":   result.fear_greed_score,
            "headlines":          result.headlines,
            "confidence":         result.confidence,
            "timestamp":          result.timestamp,
        }
