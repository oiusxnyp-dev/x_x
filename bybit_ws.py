import threading
import time

from pybit.unified_trading import WebSocket

from bybit_credentials import get_bybit_credentials
from get_session import get_session
from realtime import realtime


class BybitRealtime:
    def __init__(self, user_id: int):
        self.user_id = int(user_id)

        self.private_ws = None
        self.public_ws = None

        self._ticker_symbols = set()
        self._started = False
        self._lock = threading.RLock()

    def bootstrap(self):
        session = get_session(self.user_id)

        if session is None:
            raise RuntimeError(
                f"Bybit session unavailable user_id={self.user_id}"
            )

        wallet_result = session.get_wallet_balance(
            accountType="UNIFIED",
        )

        wallet = wallet_result["result"]["list"][0]

        realtime.set_wallet(
            self.user_id,
            {
                "equity": wallet.get("totalEquity"),
                "wallet": wallet.get("totalWalletBalance"),
                "available": wallet.get("totalAvailableBalance"),
                "unrealised": wallet.get("totalPerpUPL"),
            },
        )

        position_result = session.get_positions(
            category="linear",
            settleCoin="USDT",
        )

        realtime.replace_positions(
            self.user_id,
            position_result["result"]["list"],
        )

    def _on_wallet(self, message):
        data = message.get("data") or []

        if not data:
            return

        wallet = data[0]

        realtime.set_wallet(
            self.user_id,
            {
                "equity": wallet.get("totalEquity"),
                "wallet": wallet.get("totalWalletBalance"),
                "available": wallet.get("totalAvailableBalance"),
                "unrealised": wallet.get("totalPerpUPL"),
            },
        )

    def _on_position(self, message):
        for p in message.get("data") or []:
            if p.get("category") not in (None, "linear"):
                continue

            realtime.update_position(
                self.user_id,
                p,
            )

        self._ensure_tickers()

    def _on_ticker(self, message):
        topic = str(message.get("topic") or "")
        data = message.get("data") or {}

        symbol = data.get("symbol")

        if not symbol and "." in topic:
            symbol = topic.rsplit(".", 1)[-1]

        if not symbol:
            return

        price = (
            data.get("markPrice")
            or data.get("lastPrice")
        )

        if price is None:
            return

        realtime.set_price(
            self.user_id,
            symbol,
            price,
        )

    def _ensure_tickers(self):
        symbols = set(realtime.symbols(self.user_id))
        missing = symbols - self._ticker_symbols

        if not missing:
            return

        # pybit에서 추가 ticker subscription을 호출할 수 있으므로
        # 새로 생긴 보유 종목만 추가 구독한다.
        for symbol in sorted(missing):
            self.public_ws.ticker_stream(
                symbol=symbol,
                callback=self._on_ticker,
            )

            self._ticker_symbols.add(symbol)

            print(
                "[BYBIT WS] "
                f"user_id={self.user_id} "
                f"ticker={symbol}",
                flush=True,
            )

    def start(self):
        with self._lock:
            if self._started:
                return

            self.bootstrap()

            credentials = get_bybit_credentials(
                self.user_id
            )

            if not credentials:
                raise RuntimeError(
                    f"Bybit credentials unavailable "
                    f"user_id={self.user_id}"
                )

            self.private_ws = WebSocket(
                testnet=False,
                channel_type="private",
                api_key=credentials["api_key"],
                api_secret=credentials["api_secret"],
            )

            self.public_ws = WebSocket(
                testnet=False,
                channel_type="linear",
            )

            self.private_ws.wallet_stream(
                callback=self._on_wallet,
            )

            self.private_ws.position_stream(
                callback=self._on_position,
            )

            self._ensure_tickers()

            self._started = True

            print(
                "[BYBIT WS] "
                f"user_id={self.user_id} started",
                flush=True,
            )


_instances = {}
_instances_lock = threading.RLock()


def ensure_bybit_realtime(user_id: int):
    uid = int(user_id)

    with _instances_lock:
        instance = _instances.get(uid)

        if instance is None:
            instance = BybitRealtime(uid)
            _instances[uid] = instance

    instance.start()

    return instance
