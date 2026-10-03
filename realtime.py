import threading
from decimal import Decimal


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

            state["positions"][key] = {
                "symbol": symbol,
                "positionIdx": position_idx,
                "side": str(p.get("side") or ""),
                "size": str(size),
                "avgPrice": str(p.get("avgPrice") or "0"),
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
            prices = dict(state["prices"])

        total_upl = Decimal("0")
        output_positions = []

        for p in positions:
            symbol = p["symbol"]
            side = p["side"]
            size = Decimal(p["size"])
            avg = Decimal(p["avgPrice"])

            price_text = prices.get(symbol)

            if price_text is None:
                mark = None
                upl = None
            else:
                mark = Decimal(price_text)

                if side == "Buy":
                    upl = (mark - avg) * size
                elif side == "Sell":
                    upl = (avg - mark) * size
                else:
                    upl = Decimal("0")

                total_upl += upl

            output_positions.append({
                **p,
                "markPrice": (
                    str(mark)
                    if mark is not None
                    else None
                ),
                "unrealisedPnl": (
                    str(upl)
                    if upl is not None
                    else None
                ),
            })

        return {
            "wallet": wallet,
            "unrealised": str(total_upl),
            "positions": output_positions,
        }


realtime = RealtimeState()
