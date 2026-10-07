import time
from dataclasses import dataclass

import redis

from get_session import get_session
from user_db import get_users
from surge_trading import get_surge_trailing_settings


POLL_SECONDS = 0.25
RESET_MAX_AGE_SECONDS = 300.0
POSITION_VISIBILITY_GRACE_SECONDS = 10.0


@dataclass
class TrailState:
    symbol: str
    entry_price: float
    high_price: float
    armed: bool = False
    closing: bool = False


class SurgeTrailingManager:
    def __init__(self, user_id):
        self.user_id = int(user_id)

        self.redis = redis.Redis(
            host="127.0.0.1",
            port=6379,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )

        self.states = {}

        # fresh RESET 직후 Bybit REST에 포지션이 아직
        # 보이지 않을 수 있으므로 해당 심볼만 잠시 보호한다.
        self.position_visibility_grace = {}

    def get_price(self, symbol):
        row = self.redis.hgetall(
            f"market:{symbol}"
        )

        if not row:
            return None

        try:
            updated_at = float(
                row.get("updated_at") or 0
            )
        except (TypeError, ValueError):
            return None

        if updated_at <= 0:
            return None

        age = max(
            0.0,
            time.time() - updated_at,
        )

        # 오래된 가격으로 청산 판단 금지
        if age > 5.0:
            return None

        raw = (
            row.get("markPrice")
            or row.get("lastPrice")
        )

        try:
            price = float(raw or 0)
        except (TypeError, ValueError):
            return None

        if price <= 0:
            return None

        return price

    def get_active_key(self):
        return (
            f"surge:trail:active:{self.user_id}"
        )

    def get_active_symbols(self):
        """
        실제 급등 주문 접수 후 reset event를 받은
        종목만 trailing 관리 대상으로 사용한다.
        """
        try:
            rows = self.redis.smembers(
                self.get_active_key()
            )
        except redis.RedisError as exc:
            print(
                "[SURGE TRAIL ACTIVE READ ERROR]",
                repr(exc),
                flush=True,
            )
            return set()

        return {
            str(symbol).upper().strip()
            for symbol in rows
            if symbol
        }

    def activate_symbol(self, symbol):
        symbol = str(symbol).upper().strip()

        if not symbol:
            return

        self.redis.sadd(
            self.get_active_key(),
            symbol,
        )

        print(
            "[SURGE TRAIL ACTIVE ADD]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            flush=True,
        )

    def deactivate_symbol(self, symbol):
        symbol = str(symbol).upper().strip()

        if not symbol:
            return

        self.redis.srem(
            self.get_active_key(),
            symbol,
        )

        self.states.pop(
            symbol,
            None,
        )

        self.position_visibility_grace.pop(
            symbol,
            None,
        )

        print(
            "[SURGE TRAIL ACTIVE REMOVE]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            flush=True,
        )

    def consume_reset_event(self):
        """
        surge_trading.py가 실제 급등 주문 접수 후 발행한
        Redis trailing reset event를 정확히 한 번 소비한다.
        """

        key = (
            f"surge:trail:reset:{self.user_id}"
        )

        try:
            # Redis 6.2+ GETDEL:
            # 값을 읽는 동시에 삭제하여 서비스 재시작 후
            # 과거 reset event가 다시 실행되는 것을 막는다.
            value = self.redis.getdel(key)
        except redis.RedisError as exc:
            print(
                "[SURGE TRAIL RESET READ ERROR]",
                f"user_id={self.user_id}",
                repr(exc),
                flush=True,
            )
            return

        if not value:
            return

        try:
            symbol, event_time_raw = (
                value.rsplit(":", 1)
            )

            symbol = (
                str(symbol)
                .upper()
                .strip()
            )

            event_time = float(
                event_time_raw
            )

            if not symbol:
                raise ValueError(
                    "empty reset symbol"
                )

        except Exception:
            print(
                "[SURGE TRAIL RESET INVALID]",
                f"user_id={self.user_id}",
                repr(value),
                flush=True,
            )
            return

        age = max(
            0.0,
            time.time() - event_time,
        )

        if age > RESET_MAX_AGE_SECONDS:
            print(
                "[SURGE TRAIL RESET STALE]",
                f"user_id={self.user_id}",
                f"symbol={symbol}",
                f"age={age:.3f}",
                f"max_age={RESET_MAX_AGE_SECONDS}",
                flush=True,
            )
            return

        # 실제 급등 주문이 정상 접수된 직후의
        # fresh event만 trailing 대상으로 등록한다.
        self.activate_symbol(symbol)

        # 주문 접수 직후 Bybit REST position 반영 지연으로
        # ACTIVE가 즉시 삭제되는 race를 막는다.
        self.position_visibility_grace[
            symbol
        ] = time.time()

        print(
            "[SURGE TRAIL POSITION GRACE START]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            f"seconds={POSITION_VISIBILITY_GRACE_SECONDS}",
            flush=True,
        )

        # 같은 종목의 기존 trailing cycle이 있다면 제거.
        # 다음 포지션 조회에서 실제 Bybit avgPrice로 다시 시작한다.
        self.reset_symbol(symbol)

        print(
            "[SURGE TRAIL RESET CONSUMED]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            f"age={age:.3f}",
            f"event={value}",
            flush=True,
        )

    def keep_active_without_position(
        self,
        symbol,
    ):
        symbol = str(symbol).upper().strip()

        started_at = (
            self.position_visibility_grace.get(
                symbol
            )
        )

        if started_at is None:
            return False

        elapsed = max(
            0.0,
            time.time() - started_at,
        )

        if (
            elapsed
            < POSITION_VISIBILITY_GRACE_SECONDS
        ):
            return True

        self.position_visibility_grace.pop(
            symbol,
            None,
        )

        print(
            "[SURGE TRAIL POSITION GRACE EXPIRED]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            f"elapsed={elapsed:.3f}",
            flush=True,
        )

        return False

    def mark_position_visible(
        self,
        symbol,
    ):
        symbol = str(symbol).upper().strip()

        started_at = (
            self.position_visibility_grace.pop(
                symbol,
                None,
            )
        )

        if started_at is None:
            return

        elapsed = max(
            0.0,
            time.time() - started_at,
        )

        print(
            "[SURGE TRAIL POSITION VISIBLE]",
            f"user_id={self.user_id}",
            f"symbol={symbol}",
            f"elapsed={elapsed:.3f}",
            flush=True,
        )

    def reset_symbol(self, symbol):
        """
        새 급등 신호가 실제 주문에 사용될 때
        해당 심볼의 이전 trailing cycle을 초기화한다.
        """
        symbol = str(symbol).upper().strip()

        old_state = self.states.pop(
            symbol,
            None,
        )

        print(
            "[SURGE TRAIL RESET]",
            f"symbol={symbol}",
            f"had_state={old_state is not None}",
            flush=True,
        )

    def get_long_positions(self):
        session = get_session(
            self.user_id
        )

        if session is None:
            raise RuntimeError(
                "Bybit session not found"
            )

        result = session.get_positions(
            category="linear",
            settleCoin="USDT",
        )

        rows = (
            result
            .get("result", {})
            .get("list", [])
        )

        active_symbols = (
            self.get_active_symbols()
        )

        positions = {}

        for row in rows:
            try:
                size = float(
                    row.get("size") or 0
                )

                position_idx = int(
                    row.get("positionIdx") or 0
                )

                avg_price = float(
                    row.get("avgPrice") or 0
                )
            except (TypeError, ValueError):
                continue

            if position_idx != 1:
                continue

            if size <= 0:
                continue

            if avg_price <= 0:
                continue

            symbol = str(
                row.get("symbol") or ""
            ).upper()

            if not symbol:
                continue

            # 급등 주문 접수 후 active로 등록된 종목만 관리한다.
            # 기존 수동 LONG / 다른 전략 LONG은 여기서 제외된다.
            if symbol not in active_symbols:
                continue

            positions[symbol] = {
                "symbol": symbol,
                "size": size,
                "avg_price": avg_price,
            }

        return positions

    def close_long(
        self,
        symbol,
        size,
    ):
        session = get_session(
            self.user_id
        )

        if session is None:
            raise RuntimeError(
                "Bybit session not found"
            )

        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": str(size),
            "positionIdx": 1,
            "reduceOnly": True,
        }

        print(
            "[SURGE TRAIL CLOSE]",
            payload,
            flush=True,
        )

        response = session.place_order(
            **payload
        )

        if not isinstance(response, dict):
            raise RuntimeError(
                "Invalid close response"
            )

        ret_code = response.get(
            "retCode"
        )

        try:
            ret_code = int(ret_code)
        except (TypeError, ValueError):
            ret_code = None

        if ret_code != 0:
            raise RuntimeError(
                "Trailing close rejected: "
                f"{response}"
            )

        return response

    def update_position(
        self,
        position,
    ):
        symbol = position["symbol"]
        size = position["size"]
        entry = position["avg_price"]

        price = self.get_price(
            symbol
        )

        if price is None:
            return

        state = self.states.get(
            symbol
        )

        if state is None:
            state = TrailState(
                symbol=symbol,
                entry_price=entry,
                high_price=price,
            )

            self.states[symbol] = state

            print(
                "[SURGE TRAIL TRACK]",
                f"symbol={symbol}",
                f"entry={entry}",
                f"price={price}",
                flush=True,
            )

        # Bybit의 실제 avgPrice가 바뀌면
        # 추가진입으로 간주하여 기준 진입가 갱신.
        if abs(
            state.entry_price - entry
        ) > 1e-12:
            print(
                "[SURGE TRAIL ENTRY UPDATE]",
                f"symbol={symbol}",
                f"old={state.entry_price}",
                f"new={entry}",
                flush=True,
            )

            state.entry_price = entry

            # 아직 ARM 전이라면 현재가부터 다시 추적.
            if not state.armed:
                state.high_price = price

        trailing_settings = (
            get_surge_trailing_settings(
                self.user_id
            )
        )

        arm_percent = (
            float(
                trailing_settings[
                    "arm_percent"
                ]
            )
            / 100.0
        )

        gap_percent = (
            float(
                trailing_settings[
                    "gap_percent"
                ]
            )
            / 100.0
        )

        arm_price = (
            state.entry_price
            * (1.0 + arm_percent)
        )

        if not state.armed:
            if price >= arm_price:
                state.armed = True
                state.high_price = price

                print(
                    "[SURGE TRAIL ARMED]",
                    f"symbol={symbol}",
                    f"entry={state.entry_price}",
                    f"price={price}",
                    f"arm={arm_price}",
                    flush=True,
                )
            else:
                return

        if price > state.high_price:
            state.high_price = price

            print(
                "[SURGE TRAIL HIGH]",
                f"symbol={symbol}",
                f"high={state.high_price}",
                flush=True,
            )

        trigger_price = (
            state.high_price
            * (1.0 - gap_percent)
        )

        if (
            price <= trigger_price
            and not state.closing
        ):
            state.closing = True

            print(
                "[SURGE TRAIL TRIGGER]",
                f"symbol={symbol}",
                f"entry={state.entry_price}",
                f"high={state.high_price}",
                f"price={price}",
                f"trigger={trigger_price}",
                f"size={size}",
                flush=True,
            )

            try:
                response = self.close_long(
                    symbol,
                    size,
                )

                print(
                    "[SURGE TRAIL CLOSED]",
                    f"symbol={symbol}",
                    f"response={response}",
                    flush=True,
                )

            except Exception:
                # 실패하면 다음 루프에서 재시도 가능.
                state.closing = False
                raise

    def run(self):
        print(
            "=" * 80,
            flush=True,
        )
        print(
            "SURGE TRAILING STARTED",
            flush=True,
        )
        print(
            f"USER_ID = {self.user_id}",
            flush=True,
        )
        settings = get_surge_trailing_settings(
            self.user_id
        )

        print(
            "ARM     = "
            f"+{settings['arm_percent']}%",
            flush=True,
        )
        print(
            "TRAIL   = HIGH -"
            f"{settings['gap_percent']}%",
            flush=True,
        )
        print(
            "=" * 80,
            flush=True,
        )

        while True:
            try:
                # 새 급등 진입이 있었다면 기존 trailing state를
                # 먼저 제거한다. 이후 get_long_positions()에서
                # 실제 Bybit avgPrice를 읽어 새 cycle을 시작한다.
                self.consume_reset_event()

                positions = (
                    self.get_long_positions()
                )

                # active로 등록되어 있었지만 실제 LONG이 사라졌다면
                # 해당 급등 trailing cycle은 완전히 종료한다.
                active_symbols = (
                    self.get_active_symbols()
                )

                for symbol in active_symbols:
                    if symbol in positions:
                        self.mark_position_visible(
                            symbol
                        )
                        continue

                    if self.keep_active_without_position(
                        symbol
                    ):
                        continue

                    print(
                        "[SURGE TRAIL REMOVE]",
                        symbol,
                        flush=True,
                    )

                    self.deactivate_symbol(
                        symbol
                    )

                for position in (
                    positions.values()
                ):
                    try:
                        self.update_position(
                            position
                        )
                    except Exception as exc:
                        print(
                            "[SURGE TRAIL POSITION ERROR]",
                            position["symbol"],
                            repr(exc),
                            flush=True,
                        )

            except Exception as exc:
                print(
                    "[SURGE TRAIL LOOP ERROR]",
                    repr(exc),
                    flush=True,
                )

            time.sleep(
                POLL_SECONDS
            )


def get_trailing_user_ids():
    user_ids = []

    for user in get_users():
        try:
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError):
            continue

        if not user.get("approved"):
            continue

        if not user.get("enabled"):
            continue

        if (
            str(
                user.get("exchange") or ""
            ).upper()
            != "BYBIT"
        ):
            continue

        try:
            if get_session(user_id) is None:
                continue
        except Exception as exc:
            print(
                "[SURGE TRAIL USER SKIP]",
                f"user_id={user_id}",
                repr(exc),
                flush=True,
            )
            continue

        user_ids.append(user_id)

    return sorted(user_ids)


def run_multi_user():
    user_ids = get_trailing_user_ids()

    print(
        "[SURGE TRAIL USERS]",
        user_ids,
        flush=True,
    )

    if not user_ids:
        raise RuntimeError(
            "No eligible Bybit trailing users"
        )

    managers = {
        user_id: SurgeTrailingManager(
            user_id
        )
        for user_id in user_ids
    }

    for manager in managers.values():
        print(
            "=" * 80,
            flush=True,
        )
        print(
            "SURGE TRAILING USER READY",
            f"user_id={manager.user_id}",
            flush=True,
        )

        settings = (
            get_surge_trailing_settings(
                manager.user_id
            )
        )

        print(
            "ARM     = "
            f"+{settings['arm_percent']}%",
            flush=True,
        )
        print(
            "TRAIL   = HIGH -"
            f"{settings['gap_percent']}%",
            flush=True,
        )

    print(
        "=" * 80,
        flush=True,
    )

    while True:
        for user_id, manager in (
            managers.items()
        ):
            try:
                manager.consume_reset_event()

                positions = (
                    manager.get_long_positions()
                )

                active_symbols = (
                    manager.get_active_symbols()
                )

                for symbol in active_symbols:
                    if symbol in positions:
                        manager.mark_position_visible(
                            symbol
                        )
                        continue

                    if manager.keep_active_without_position(
                        symbol
                    ):
                        continue

                    print(
                        "[SURGE TRAIL REMOVE]",
                        f"user_id={user_id}",
                        symbol,
                        flush=True,
                    )

                    manager.deactivate_symbol(
                        symbol
                    )

                for position in (
                    positions.values()
                ):
                    try:
                        manager.update_position(
                            position
                        )
                    except Exception as exc:
                        print(
                            "[SURGE TRAIL POSITION ERROR]",
                            f"user_id={user_id}",
                            position["symbol"],
                            repr(exc),
                            flush=True,
                        )

            except Exception as exc:
                print(
                    "[SURGE TRAIL USER ERROR]",
                    f"user_id={user_id}",
                    repr(exc),
                    flush=True,
                )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_multi_user()
