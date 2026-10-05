import threading
from decimal import Decimal

import redis


_market_redis = redis.Redis(
    host="127.0.0.1",
    port=6379,
    decode_responses=True,
)


class RealtimeState:
    def __init__(self):
        self._lock = threading.RLock()
        self._users = {}

    def _user(self, user_id):
        uid = int(user_id)

        with self._lock:
            return self._users.setdefault(
                uid,
                {
                    "wallet": {},
                    "positions": {},
                    "prices": {},
                },
            )

    def set_wallet(self, user_id, wallet):
        with self._lock:
            state = self._user(user_id)
            state["wallet"].update(wallet)

    def replace_positions(self, user_id, positions):
        new_positions = {}

        for p in positions:
            size = Decimal(str(p.get("size") or "0"))

            if size <= 0:
                continue

            symbol = str(p["symbol"])
            position_idx = int(p.get("positionIdx") or 0)

            new_positions[(symbol, position_idx)] = {
                "symbol": symbol,
                "positionIdx": position_idx,
                "side": str(p.get("side") or ""),
                "size": str(size),
                "avgPrice": str(p.get("avgPrice") or "0"),
            }

        with self._lock:
            state = self._user(user_id)
            state["positions"] = new_positions

    def update_position(self, user_id, p):
        symbol = str(p.get("symbol") or "")
        position_idx = int(p.get("positionIdx") or 0)

        if not symbol:
            return

        key = (symbol, position_idx)
        size = Decimal(str(p.get("size") or "0"))

        with self._lock:
            state = self._user(user_id)

            if size <= 0:
                state["positions"].pop(key, None)
                return

            existing = state["positions"].get(
                key,
                {},
            )

            side = p.get("side")
            avg_price = p.get("avgPrice")

            state["positions"][key] = {
                "symbol": symbol,
                "positionIdx": position_idx,
                "side": (
                    str(side)
                    if side not in (None, "")
                    else existing.get("side", "")
                ),
                "size": str(size),
                "avgPrice": (
                    str(avg_price)
                    if avg_price not in (None, "", "0")
                    else existing.get("avgPrice", "0")
                ),
            }

    def set_price(self, user_id, symbol, price):
        if price is None:
            return

        with self._lock:
            state = self._user(user_id)
            state["prices"][str(symbol)] = str(price)

    def symbols(self, user_id):
        with self._lock:
            state = self._user(user_id)

            return sorted({
                p["symbol"]
                for p in state["positions"].values()
            })

    def snapshot(self, user_id):
        with self._lock:
            state = self._user(user_id)

            wallet = dict(state["wallet"])
            positions = [
                dict(p)
                for p in state["positions"].values()
            ]
        total_upl = Decimal("0")
        output_positions = []
        missing_lastprice = False

        for p in positions:
            symbol = p["symbol"]
            side = p["side"]
            size = Decimal(p["size"])
            avg = Decimal(p["avgPrice"])

            price_text = _market_redis.hget(
                f"market:{symbol}",
                "lastPrice",
            )

            if price_text is None:
                last = None
                upl = None
                missing_lastprice = True
            else:
                last = Decimal(price_text)

                if side == "Buy":
                    upl = (last - avg) * size
                elif side == "Sell":
                    upl = (avg - last) * size
                else:
                    upl = Decimal("0")

                total_upl += upl

            output_positions.append({
                **p,
                "lastPrice": (
                    str(last)
                    if last is not None
                    else None
                ),
                "unrealisedPnl": (
                    str(upl)
                    if upl is not None
                    else None
                ),
            })

        # 상단 계좌 정보는 Bybit wallet 원본을 그대로 사용한다.
        #
        # equity     = Bybit totalEquity
        # wallet     = Bybit totalWalletBalance
        # available  = Bybit totalAvailableBalance
        # unrealised = Bybit totalPerpUPL
        #
        # ticker 기반 계산값은 아래 position별
        # lastPrice / unrealisedPnl 표시에만 사용한다.
        display_unrealised = wallet.get(
            "unrealised"
        )

        return {
            "wallet": wallet,
            "unrealised": display_unrealised,
            "positions": output_positions,
        }


class MarketPriceState:
    """
    Bybit public ticker 공용 가격 캐시.

    public market data는 user별 데이터가 아니므로
    모든 user / trading / dashboard가 하나의 cache를 공유한다.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._prices = {}

    def update(
        self,
        symbol,
        *,
        mark_price=None,
        last_price=None,
        bid1_price=None,
        ask1_price=None,
        updated_at=None,
    ):
        import time

        symbol = str(symbol or "").upper().strip()

        if not symbol:
            return

        now = (
            float(updated_at)
            if updated_at is not None
            else time.time()
        )

        with self._lock:
            row = self._prices.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "markPrice": None,
                    "lastPrice": None,
                    "bid1Price": None,
                    "ask1Price": None,
                    "updated_at": 0.0,
                },
            )

            # ticker는 delta일 수 있으므로
            # 이번 message에 존재하는 값만 갱신한다.
            if mark_price is not None:
                row["markPrice"] = str(mark_price)

            if last_price is not None:
                row["lastPrice"] = str(last_price)

            if bid1_price is not None:
                row["bid1Price"] = str(bid1_price)

            if ask1_price is not None:
                row["ask1Price"] = str(ask1_price)

            row["updated_at"] = now

    def get(self, symbol):
        symbol = str(symbol or "").upper().strip()

        with self._lock:
            row = self._prices.get(symbol)

            if row is None:
                return None

            return dict(row)

    def price(
        self,
        symbol,
        *,
        prefer="markPrice",
    ):
        row = self.get(symbol)

        if row is None:
            return None

        order = [
            prefer,
            "markPrice",
            "lastPrice",
            "bid1Price",
            "ask1Price",
        ]

        seen = set()

        for key in order:
            if key in seen:
                continue

            seen.add(key)

            value = row.get(key)

            if value in (None, ""):
                continue

            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            if value > 0:
                return value

        return None

    def age(self, symbol):
        import time

        row = self.get(symbol)

        if row is None:
            return None

        updated_at = float(
            row.get("updated_at") or 0
        )

        if updated_at <= 0:
            return None

        return max(
            0.0,
            time.time() - updated_at,
        )

    def symbols(self):
        with self._lock:
            return sorted(self._prices)

    def count(self):
        with self._lock:
            return len(self._prices)


market_prices = MarketPriceState()


realtime = RealtimeState()
