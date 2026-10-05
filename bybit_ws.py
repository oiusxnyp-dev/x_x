import threading
import time

from pybit.unified_trading import WebSocket

from bybit_credentials import get_bybit_credentials
from get_session import get_session
from realtime import realtime, market_prices
from surge_trading import get_cached_linear_symbols


class BybitRealtime:
    def __init__(self, user_id: int):
        self.user_id = int(user_id)

        self.private_ws = None
        self.public_ws = None

        self._ticker_symbols = set()
        self._started = False
        self._lock = threading.RLock()

        # Position WS는 빠른 화면 갱신용으로 사용하고,
        # 최종 size / side / avgPrice는 Bybit REST와 재동기화한다.
        # 연속 체결 시 REST 호출이 폭증하지 않도록 debounce한다.
        self._position_reconcile_timer = None
        self._position_reconcile_lock = threading.RLock()

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

        positions = position_result["result"]["list"]

        realtime.replace_positions(
            self.user_id,
            positions,
        )

        # 첫 WebSocket markPrice 이벤트 전에도
        # REST bootstrap의 Mark Price를 초기값으로 사용한다.
        for p in positions:
            symbol = p.get("symbol")
            mark_price = p.get("markPrice")

            if symbol and mark_price:
                realtime.set_price(
                    self.user_id,
                    symbol,
                    mark_price,
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

    def _reconcile_positions_from_rest(self):
        with self._position_reconcile_lock:
            self._position_reconcile_timer = None

        try:
            session = get_session(self.user_id)

            if session is None:
                raise RuntimeError(
                    f"Bybit session unavailable user_id={self.user_id}"
                )

            result = session.get_positions(
                category="linear",
                settleCoin="USDT",
            )

            positions = result["result"]["list"]

            realtime.replace_positions(
                self.user_id,
                positions,
            )

            print(
                "[BYBIT POSITION RECONCILE] "
                f"user_id={self.user_id} "
                f"positions={sum(1 for p in positions if float(p.get('size') or 0) > 0)}",
                flush=True,
            )

        except Exception as exc:
            print(
                "[BYBIT POSITION RECONCILE] "
                f"user_id={self.user_id} "
                f"error={exc}",
                flush=True,
            )

    def _schedule_position_reconcile(self):
        with self._position_reconcile_lock:
            timer = self._position_reconcile_timer

            if timer is not None:
                timer.cancel()

            timer = threading.Timer(
                0.5,
                self._reconcile_positions_from_rest,
            )
            timer.daemon = True

            self._position_reconcile_timer = timer
            timer.start()

    def _on_position(self, message):
        for p in message.get("data") or []:
            if p.get("category") not in (None, "linear"):
                continue

            realtime.update_position(
                self.user_id,
                p,
            )

        self._ensure_tickers()
        self._schedule_position_reconcile()

    def _on_ticker(self, message):
        topic = str(message.get("topic") or "")
        data = message.get("data") or {}

        symbol = data.get("symbol")

        if not symbol and "." in topic:
            symbol = topic.rsplit(".", 1)[-1]

        if not symbol:
            return

        # Public ticker는 모든 user가 공유하는 market cache에 저장.
        # ticker message는 delta일 수 있으므로 cache가 이전 필드를 유지한다.
        market_prices.update(
            symbol,
            mark_price=data.get("markPrice"),
            last_price=data.get("lastPrice"),
            bid1_price=data.get("bid1Price"),
            ask1_price=data.get("ask1Price"),
        )

        # 기존 dashboard와의 호환성.
        # markPrice가 들어온 이벤트에서만 user realtime price도 갱신한다.
        mark_price = data.get("markPrice")

        if mark_price is not None:
            realtime.set_price(
                self.user_id,
                symbol,
                mark_price,
            )

    def _ensure_tickers(self):
        # instruments.db에 저장된 Trading USDT Linear 전 종목을
        # public ticker로 상시 구독한다.
        symbols = set(
            get_cached_linear_symbols()
        )

        missing = symbols - self._ticker_symbols

        if not missing:
            return

        for symbol in sorted(missing):
            self.public_ws.ticker_stream(
                symbol=symbol,
                callback=self._on_ticker,
            )

            self._ticker_symbols.add(symbol)

        print(
            "[BYBIT WS] "
            f"global tickers subscribed="
            f"{len(self._ticker_symbols)}",
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
