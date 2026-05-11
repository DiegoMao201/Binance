from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

import ccxt
import pandas as pd

from src.utils.config import Settings


T = TypeVar("T")


class BinanceClientError(Exception):
    def __init__(self, message: str, *, category: str = "exchange_other") -> None:
        super().__init__(message)
        self.category = category


def classify_binance_error(exc: Exception | str) -> str:
    message = str(exc).lower()

    if "-2015" in message or "invalid api-key" in message or "invalid api key" in message:
        return "auth_invalid"
    if "-2014" in message or "api-key format invalid" in message:
        return "auth_invalid"
    if "-2008" in message or "invalid api-key id" in message:
        return "auth_invalid"
    if "signature for this request is not valid" in message or "-1022" in message:
        return "auth_invalid"
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return "rate_limit"
    if "timed out" in message or "timeout" in message or "requesttimeout" in message:
        return "timeout_binance"
    if any(token in message for token in [
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
        "connection aborted",
        "connection reset",
        "network is unreachable",
        "nodename nor servname provided",
        "max retries exceeded",
        "remote end closed connection",
        "ssl",
    ]):
        return "network_local"
    return "exchange_other"


class BinanceDataClient:
    def __init__(self, settings: Settings) -> None:
        public_config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 8000,
            "options": {"defaultType": "spot"},
        }
        if settings.binance_proxy_url:
            public_config["proxies"] = {
                "http": settings.binance_proxy_url,
                "https": settings.binance_proxy_url,
            }
        self.public_exchange = ccxt.binance(public_config)

        private_config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 8000,
            "options": {"defaultType": "spot"},
        }
        if settings.binance_proxy_url:
            private_config["proxies"] = {
                "http": settings.binance_proxy_url,
                "https": settings.binance_proxy_url,
            }
        if settings.binance_api_key and settings.binance_api_secret:
            private_config.update(
                {
                    "apiKey": settings.binance_api_key,
                    "secret": settings.binance_api_secret,
                }
            )

        self.private_exchange = ccxt.binance(private_config)
        self.settings = settings

    def _ensure_market_loaded(self, symbol: str) -> None:
        self._with_retries(lambda: self.private_exchange.load_markets())
        if symbol not in (self.private_exchange.markets or {}):
            raise BinanceClientError(f"Mercado no disponible en Binance: {symbol}")

    @staticmethod
    def _with_retries(operation: Callable[[], T], attempts: int = 4, base_delay: float = 0.75) -> T:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except ccxt.RequestTimeout as exc:
                last_error = exc
                time.sleep(base_delay * (2 ** (attempt - 1)))
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                last_error = exc
                time.sleep(base_delay * (2 ** (attempt - 1)))
            except ccxt.BaseError as exc:
                raise BinanceClientError(str(exc), category=classify_binance_error(exc)) from exc
        raise BinanceClientError(
            f"Binance no respondió tras {attempts} intentos: {last_error}",
            category=classify_binance_error(last_error or "timeout"),
        )

    @property
    def base_asset(self) -> str:
        symbol = self.settings.trading_symbol
        return symbol.split("/")[0] if "/" in symbol else symbol

    @property
    def quote_asset(self) -> str:
        symbol = self.settings.trading_symbol
        return symbol.split("/")[1] if "/" in symbol else "USDT"

    def base_asset_for(self, symbol: str | None = None) -> str:
        target = symbol or self.settings.trading_symbol
        return target.split("/")[0] if "/" in target else target

    def quote_asset_for(self, symbol: str | None = None) -> str:
        target = symbol or self.settings.trading_symbol
        return target.split("/")[1] if "/" in target else "USDT"

    def fetch_ohlcv(self, limit: int = 200, symbol: str | None = None) -> pd.DataFrame:
        target = symbol or self.settings.trading_symbol
        candles = self._with_retries(
            lambda: self.public_exchange.fetch_ohlcv(
                target,
                timeframe=self.settings.timeframe,
                limit=limit,
            )
        )
        frame = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        return frame

    def _fetch_spot_account(self) -> dict[str, Any]:
        return self._with_retries(lambda: self.private_exchange.privateGetAccount())

    def _fetch_full_balance(self) -> dict[str, Any]:
        account = self._fetch_spot_account()
        balances = account.get("balances", []) or []
        normalized: dict[str, Any] = {"info": account}
        free_bucket: dict[str, float] = {}
        used_bucket: dict[str, float] = {}
        total_bucket: dict[str, float] = {}

        for entry in balances:
            asset = str(entry.get("asset") or "").upper()
            if not asset:
                continue
            free = float(entry.get("free", 0.0) or 0.0)
            locked = float(entry.get("locked", 0.0) or 0.0)
            total = free + locked
            normalized[asset] = {
                "free": free,
                "used": locked,
                "total": total,
            }
            free_bucket[asset] = free
            used_bucket[asset] = locked
            total_bucket[asset] = total

        normalized["free"] = free_bucket
        normalized["used"] = used_bucket
        normalized["total"] = total_bucket
        return normalized

    @staticmethod
    def _balance_aliases(asset: str) -> tuple[str, ...]:
        # Stablecoins distintos no son intercambiables al ejecutar pares /USDT;
        # leer USD/FDUSD/USDC como si fueran USDT provoca rejects en Binance.
        return (asset,)

    def _extract_balance_value(self, balance: dict[str, Any], asset: str, field: str) -> float:
        for candidate in self._balance_aliases(asset):
            info = balance.get(candidate, {}) or {}
            value = info.get(field)
            if value not in (None, 0, 0.0):
                return float(value)

        bucket = balance.get(field, {}) or {}
        for candidate in self._balance_aliases(asset):
            value = bucket.get(candidate)
            if value not in (None, 0, 0.0):
                return float(value)

        return 0.0

    def fetch_balance_usd(self) -> float:
        if self.settings.dry_run or not self.settings.binance_api_key:
            return self.settings.initial_capital_usd

        balance = self._fetch_full_balance()
        return self._extract_balance_value(balance, "USDT", "free")

    def fetch_asset_balance(self, asset: str | None = None, symbol: str | None = None) -> dict[str, float]:
        target = asset or self.base_asset_for(symbol)
        if self.settings.dry_run or not self.settings.binance_api_key:
            return {"asset": target, "free": 0.0, "used": 0.0, "total": 0.0}

        balance = self._fetch_full_balance()
        return {
            "asset": target,
            "free": self._extract_balance_value(balance, target, "free"),
            "used": self._extract_balance_value(balance, target, "used"),
            "total": self._extract_balance_value(balance, target, "total"),
        }

    def fetch_ticker_price(self, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        ticker = self._with_retries(lambda: self.public_exchange.fetch_ticker(target))
        return float(ticker.get("last") or ticker.get("close") or 0.0)

    def fetch_recent_trades(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._with_retries(lambda: self.private_exchange.fetch_my_trades(symbol, limit=limit))

    def fetch_orderbook_snapshot(self, symbol: str | None = None, depth: int = 20) -> dict[str, Any]:
        """Lee el orderbook spot publico y resume metricas utiles para scalping.

        Devuelve un dict con:
            - bid, ask, mid, spread_pct
            - bid_volume, ask_volume (suma de los primeros `depth` niveles)
            - imbalance: bid_volume / (bid_volume + ask_volume) en [0,1]
                         > 0.55 = presion compradora; < 0.45 = presion vendedora.
            - top_bid_size, top_ask_size
        Nunca lanza: ante error devuelve {"error": str}.
        """
        target = symbol or self.settings.trading_symbol
        try:
            book = self._with_retries(
                lambda: self.public_exchange.fetch_order_book(target, limit=depth)
            )
        except BinanceClientError as exc:
            return {"error": str(exc)}

        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return {"error": "orderbook vacio"}

        top_bid_price, top_bid_size = float(bids[0][0]), float(bids[0][1])
        top_ask_price, top_ask_size = float(asks[0][0]), float(asks[0][1])
        bid_volume = sum(float(level[1]) for level in bids[:depth])
        ask_volume = sum(float(level[1]) for level in asks[:depth])
        total_volume = bid_volume + ask_volume
        imbalance = bid_volume / total_volume if total_volume > 0 else 0.5
        mid = (top_bid_price + top_ask_price) / 2.0
        spread_pct = (top_ask_price - top_bid_price) / mid if mid > 0 else 0.0

        return {
            "bid": round(top_bid_price, 8),
            "ask": round(top_ask_price, 8),
            "mid": round(mid, 8),
            "spread_pct": round(spread_pct, 6),
            "bid_volume": round(bid_volume, 4),
            "ask_volume": round(ask_volume, 4),
            "imbalance": round(imbalance, 4),
            "top_bid_size": round(top_bid_size, 4),
            "top_ask_size": round(top_ask_size, 4),
            "depth_levels": depth,
        }

    def fetch_macro_regime(self, symbol: str | None = None, timeframe: str = "15m", limit: int = 100) -> dict[str, Any]:
        """Lee OHLCV de timeframe superior y resume el regimen de tendencia.

        Usa EMA20/EMA50 sobre la temporalidad mayor. Devuelve:
            - timeframe
            - close, ema20, ema50
            - trend: 'bullish' | 'bearish' | 'neutral'
            - slope_pct: pendiente normalizada de la EMA50 (proxy de fuerza)
            - close_above_ema50: bool
        Nunca lanza: ante error devuelve {"error": str}.
        """
        target = symbol or self.settings.trading_symbol
        try:
            candles = self._with_retries(
                lambda: self.public_exchange.fetch_ohlcv(target, timeframe=timeframe, limit=limit)
            )
        except BinanceClientError as exc:
            return {"timeframe": timeframe, "error": str(exc)}

        if not candles or len(candles) < 50:
            return {"timeframe": timeframe, "error": "datos insuficientes"}

        frame = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        ema20 = frame["close"].ewm(span=20, adjust=False).mean()
        ema50 = frame["close"].ewm(span=50, adjust=False).mean()
        close = float(frame["close"].iloc[-1])
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        e50_prev = float(ema50.iloc[-6]) if len(ema50) >= 6 else float(ema50.iloc[0])
        slope_pct = (e50 - e50_prev) / e50_prev if e50_prev > 0 else 0.0
        close_above_ema50 = close > e50

        if e20 > e50 and close_above_ema50 and slope_pct > 0:
            trend = "bullish"
        elif e20 < e50 and not close_above_ema50 and slope_pct < 0:
            trend = "bearish"
        else:
            trend = "neutral"

        return {
            "timeframe": timeframe,
            "close": round(close, 8),
            "ema20": round(e20, 8),
            "ema50": round(e50, 8),
            "slope_pct": round(slope_pct, 6),
            "close_above_ema50": close_above_ema50,
            "trend": trend,
        }

    def fetch_ticker_snapshot(self, symbol: str | None = None) -> dict[str, Any]:
        """Resumen de microestructura y liquidez intradia desde ticker publico.

        Nunca lanza: ante error devuelve {"error": str}.
        """
        target = symbol or self.settings.trading_symbol
        try:
            ticker = self._with_retries(lambda: self.public_exchange.fetch_ticker(target))
        except BinanceClientError as exc:
            return {"error": str(exc)}

        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        last = float(ticker.get("last") or ticker.get("close") or 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_pct = ((ask - bid) / mid) if mid > 0 and ask >= bid and bid > 0 else 0.0

        return {
            "last": round(last, 8),
            "bid": round(bid, 8),
            "ask": round(ask, 8),
            "mid": round(mid, 8),
            "spread_pct": round(spread_pct, 6),
            "price_change_pct_24h": round(float(ticker.get("percentage") or 0.0) / 100.0, 6),
            "base_volume_24h": round(float(ticker.get("baseVolume") or 0.0), 4),
            "quote_volume_24h": round(float(ticker.get("quoteVolume") or 0.0), 4),
            "vwap_24h": round(float(ticker.get("vwap") or 0.0), 8),
        }

    def fetch_trade_flow_snapshot(self, symbol: str | None = None, limit: int = 80) -> dict[str, Any]:
        """Lee el tape publico reciente y estima sesgo comprador/vendedor.

        Metricas:
            - buy_ratio: fraccion de volumen clasificado como comprador
            - momentum_pct: drift de precio en la ventana
            - flow_score: score [0,1] que combina buy_ratio + momentum
        Nunca lanza: ante error devuelve {"error": str}.
        """
        target = symbol or self.settings.trading_symbol
        try:
            trades = self._with_retries(lambda: self.public_exchange.fetch_trades(target, limit=limit))
        except BinanceClientError as exc:
            return {"error": str(exc)}

        if not trades:
            return {"error": "sin trades"}

        buy_volume = 0.0
        sell_volume = 0.0
        first_price = 0.0
        last_price = 0.0
        prev_price = None

        for idx, trade in enumerate(trades):
            try:
                price = float(trade.get("price") or 0.0)
                amount = float(trade.get("amount") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or amount <= 0:
                continue

            if idx == 0:
                first_price = price
            last_price = price

            side = str(trade.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                if prev_price is None or price >= prev_price:
                    side = "buy"
                else:
                    side = "sell"
            if side == "buy":
                buy_volume += amount
            else:
                sell_volume += amount
            prev_price = price

        total_volume = buy_volume + sell_volume
        if total_volume <= 0:
            return {"error": "volumen de tape invalido"}

        buy_ratio = buy_volume / total_volume
        momentum_pct = ((last_price - first_price) / first_price) if first_price > 0 else 0.0
        # Normaliza momentum entre 0 y 1 alrededor de +/-0.30% en la ventana.
        momentum_norm = max(0.0, min(1.0, (momentum_pct + 0.003) / 0.006))
        flow_score = (buy_ratio * 0.65) + (momentum_norm * 0.35)

        return {
            "sample_size": len(trades),
            "buy_ratio": round(buy_ratio, 4),
            "sell_ratio": round(1.0 - buy_ratio, 4),
            "buy_volume": round(buy_volume, 4),
            "sell_volume": round(sell_volume, 4),
            "momentum_pct": round(momentum_pct, 6),
            "flow_score": round(max(0.0, min(1.0, flow_score)), 4),
        }

    def ping(self) -> bool:
        try:
            self._with_retries(lambda: self.public_exchange.fetch_time())
            return True
        except BinanceClientError:
            return False

    def detect_egress_ip(self, timeout: float = 5.0) -> dict[str, Any]:
        """Best-effort: returns the public IP that Binance sees from this process.

        Uses the configured proxy when present so the result matches what Binance
        whitelists. Failures are reported in the dict and never raise.
        """
        import requests  # local import to avoid hard dep at module load

        proxies: dict[str, str] | None = None
        if self.settings.binance_proxy_url:
            proxies = {
                "http": self.settings.binance_proxy_url,
                "https": self.settings.binance_proxy_url,
            }

        result: dict[str, Any] = {
            "proxy_url_configured": bool(self.settings.binance_proxy_url),
            "egress_ip": None,
            "source": None,
            "error": None,
        }
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                response = requests.get(url, proxies=proxies, timeout=timeout)
                response.raise_for_status()
                ip = response.text.strip()
                if ip:
                    result["egress_ip"] = ip
                    result["source"] = url
                    return result
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{url}: {exc}"
        return result

    def amount_to_precision(self, amount: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.amount_to_precision(target, amount)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"amount_to_precision {target}: {exc}") from exc

    def price_to_precision(self, price: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.price_to_precision(target, price)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"price_to_precision {target}: {exc}") from exc

    def cost_to_precision(self, cost: float, symbol: str | None = None) -> float:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        try:
            formatted = self.private_exchange.cost_to_precision(target, cost)
            return float(formatted)
        except (ccxt.BaseError, ValueError, TypeError) as exc:
            raise BinanceClientError(f"cost_to_precision {target}: {exc}") from exc

    def create_market_order(
        self,
        side: str,
        amount: float,
        symbol: str | None = None,
        *,
        quote_amount: float | None = None,
    ) -> dict[str, Any]:
        target = symbol or self.settings.trading_symbol
        if side == "buy" and quote_amount is not None:
            return self._with_retries(
                lambda: self.private_exchange.create_market_buy_order_with_cost(target, quote_amount)
            )
        return self._with_retries(
            lambda: self.private_exchange.create_market_order(target, side, amount)
        )

    def cancel_all_orders_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        """Cancel ALL open orders for a symbol (used to clear stale OCOs on recovered positions)."""
        return self._with_retries(lambda: self.private_exchange.cancel_all_orders(symbol) or [])

    def cancel_order_list(
        self,
        *,
        symbol: str,
        order_list_id: str | int | None = None,
        list_client_order_id: str | None = None,
    ) -> dict[str, Any]:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        market = self.private_exchange.market(target)
        params: dict[str, Any] = {"symbol": market["id"]}
        if order_list_id is not None:
            params["orderListId"] = order_list_id
        if list_client_order_id:
            params["listClientOrderId"] = list_client_order_id

        method_candidates = (
            "privateDeleteOrderList",
            "private_delete_order_list",
            "sapiDeleteOrderList",
            "sapi_delete_order_list",
        )
        for method_name in method_candidates:
            method = getattr(self.private_exchange, method_name, None)
            if callable(method):
                return self._with_retries(lambda: method(params))

        return self._with_retries(
            lambda: self.private_exchange.request("orderList", "private", "DELETE", params)
        )

    def create_protection_oco_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> dict[str, Any]:
        target = symbol or self.settings.trading_symbol
        self._ensure_market_loaded(target)
        market = self.private_exchange.market(target)

        quantity_precise = self.amount_to_precision(quantity, symbol=target)
        take_profit_precise = self.price_to_precision(take_profit_price, symbol=target)
        stop_trigger_precise = self.price_to_precision(stop_loss_price, symbol=target)
        stop_limit_raw = stop_loss_price * (0.999 if side.lower() == "sell" else 1.001)
        stop_limit_precise = self.price_to_precision(stop_limit_raw, symbol=target)

        # Binance nueva API orderList/oco (vigente desde 2025-12):
        # Requiere aboveType/belowType en lugar de price/stopPrice/stopLimitPrice.
        # Para SELL OCO de proteccion: take-profit arriba (LIMIT_MAKER) + stop-loss abajo (STOP_LOSS_LIMIT).
        params: dict[str, Any] = {
            "symbol": market["id"],
            "side": side.upper(),
            "quantity": self.private_exchange.amount_to_precision(target, quantity_precise),
            "aboveType": "LIMIT_MAKER",
            "abovePrice": self.private_exchange.price_to_precision(target, take_profit_precise),
            "belowType": "STOP_LOSS_LIMIT",
            "belowStopPrice": self.private_exchange.price_to_precision(target, stop_trigger_precise),
            "belowPrice": self.private_exchange.price_to_precision(target, stop_limit_precise),
            "belowTimeInForce": "GTC",
        }

        method_candidates = (
            "privatePostOrderListOco",
            "private_post_order_list_oco",
            "sapiPostOrderListOco",
            "sapi_post_order_list_oco",
            "privatePostOrderOco",
            "private_post_order_oco",
            "sapiPostOrderOco",
            "sapi_post_order_oco",
        )
        for method_name in method_candidates:
            method = getattr(self.private_exchange, method_name, None)
            if callable(method):
                return self._with_retries(lambda: method(params))

        return self._with_retries(
            lambda: self.private_exchange.request("orderList/oco", "private", "POST", params)
        )