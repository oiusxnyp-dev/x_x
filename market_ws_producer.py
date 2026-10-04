import signal
import sys
import time

import redis
from pybit.unified_trading import HTTP, WebSocket

from surge_trading import get_cached_linear_symbols


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379

KEY_PREFIX = "market:"
META_KEY = "market:meta"

# WS 전체가 이 시간 이상 들어오지 않으면
# Public REST ticker snapshot으로 Redis 가격을 복구한다.
STALE_SECONDS = 5.0

# WS가 계속 죽어 있을 때 REST 호출 최소 간격.
REST_FALLBACK_INTERVAL = 2.0


class MarketWSProducer:
    def __init__(self):
        self.redis = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True,
        )

        self.ws = None
        self.symbols = []
        self.received = set()

        self.started_at = time.time()
        self.message_count = 0
        self.running = True

        # Public ticker REST.
        # 인증이 필요하지 않으며 WS 장애 복구에만 사용한다.
        self.http = HTTP(
            testnet=False,
        )

        self.last_ws_received_at = None
        self.last_rest_recovery_at = None

        self.rest_recovery_count = 0
        self.rest_written_symbols = 0

        # STARTING / WS / REST_FALLBACK
        self.source = "STARTING"

    def _meta_mapping(
        self,
        *,
        now=None,
    ):
        if now is None:
            now = time.time()

        mapping = {
            "updated_at": str(now),
            "message_count":
                str(self.message_count),
            "received_symbols":
                str(len(self.received)),
            "expected_symbols":
                str(len(self.symbols)),
            "source":
                str(self.source),
            "rest_recovery_count":
                str(self.rest_recovery_count),
            "rest_written_symbols":
                str(self.rest_written_symbols),
        }

        if self.last_ws_received_at is not None:
            mapping["ws_last_received_at"] = str(
                self.last_ws_received_at
            )

        if self.last_rest_recovery_at is not None:
            mapping["rest_last_recovery_at"] = str(
                self.last_rest_recovery_at
            )

        return mapping

    def _write_ticker(
        self,
        symbol,
        data,
    ):
        now = time.time()

        key = KEY_PREFIX + symbol

        # Bybit ticker는 delta message일 수 있다.
        # 따라서 이번 message에 실제로 존재하는 필드만
        # Redis hash에 덮어쓴다.
        mapping = {
            "symbol": symbol,
            "updated_at": str(now),
            "source": "WS",
        }

        for field in (
            "markPrice",
            "lastPrice",
            "bid1Price",
            "ask1Price",
        ):
            value = data.get(field)

            if value is not None:
                mapping[field] = str(value)

        pipe = self.redis.pipeline(
            transaction=False
        )

        pipe.hset(
            key,
            mapping=mapping,
        )

        pipe.hset(
            META_KEY,
            mapping=self._meta_mapping(
                now=now,
            ),
        )

        pipe.execute()

    def _write_rest_rows(
        self,
        rows,
    ):
        """
        Public REST ticker snapshot을 Redis에 일괄 반영한다.

        WS message_count / received 집계는 변경하지 않는다.
        """

        now = time.time()
        expected = set(self.symbols)

        pipe = self.redis.pipeline(
            transaction=False
        )

        written = 0

        for data in rows:
            symbol = str(
                data.get("symbol") or ""
            ).upper().strip()

            if (
                not symbol
                or symbol not in expected
            ):
                continue

            mapping = {
                "symbol": symbol,
                "updated_at": str(now),
                "source": "REST_FALLBACK",
            }

            for field in (
                "markPrice",
                "lastPrice",
                "bid1Price",
                "ask1Price",
            ):
                value = data.get(field)

                if value is not None:
                    mapping[field] = str(value)

            pipe.hset(
                KEY_PREFIX + symbol,
                mapping=mapping,
            )

            written += 1

        self.last_rest_recovery_at = now
        self.rest_recovery_count += 1
        self.rest_written_symbols = written
        self.source = "REST_FALLBACK"

        pipe.hset(
            META_KEY,
            mapping=self._meta_mapping(
                now=now,
            ),
        )

        pipe.execute()

        return written

    def _recover_from_rest(self):
        t0 = time.perf_counter()

        response = self.http.get_tickers(
            category="linear",
        )

        if response.get("retCode") != 0:
            raise RuntimeError(
                "Public ticker REST failed: "
                f"retCode={response.get('retCode')} "
                f"retMsg={response.get('retMsg')}"
            )

        rows = (
            response
            .get("result", {})
            .get("list", [])
        )

        if not rows:
            raise RuntimeError(
                "Public ticker REST returned no rows"
            )

        written = self._write_rest_rows(
            rows
        )

        elapsed = (
            time.perf_counter() - t0
        )

        print(
            "[MARKET PRODUCER] "
            "REST FALLBACK "
            f"written={written}/"
            f"{len(self.symbols)} "
            f"elapsed={elapsed:.3f}s "
            f"count={self.rest_recovery_count}",
            flush=True,
        )

        return written

    def _watchdog(self):
        now = time.time()

        # 첫 WS message 전에는 process 시작 시간을 기준으로 한다.
        reference = (
            self.last_ws_received_at
            if self.last_ws_received_at is not None
            else self.started_at
        )

        ws_age = now - reference

        if ws_age <= STALE_SECONDS:
            return

        # WS가 계속 죽어 있어도 REST를 과도하게 호출하지 않는다.
        if (
            self.last_rest_recovery_at is not None
            and (
                now
                - self.last_rest_recovery_at
            ) < REST_FALLBACK_INTERVAL
        ):
            return

        try:
            self._recover_from_rest()

        except Exception as exc:
            # REST 실패 자체로 producer를 종료하지 않는다.
            # 다음 watchdog cycle에서 다시 시도한다.
            print(
                "[MARKET PRODUCER] "
                "REST FALLBACK ERROR "
                f"{repr(exc)}",
                flush=True,
            )

    def _on_ticker(self, message):
        topic = str(
            message.get("topic") or ""
        )

        data = message.get("data") or {}

        symbol = data.get("symbol")

        if not symbol and "." in topic:
            symbol = topic.rsplit(
                ".",
                1,
            )[-1]

        symbol = str(
            symbol or ""
        ).upper().strip()

        if not symbol:
            return

        now = time.time()

        previous_source = self.source

        self.last_ws_received_at = now
        self.source = "WS"

        self.received.add(symbol)
        self.message_count += 1

        if previous_source == "REST_FALLBACK":
            print(
                "[MARKET PRODUCER] "
                "WS RECOVERED "
                f"symbol={symbol}",
                flush=True,
            )

        self._write_ticker(
            symbol,
            data,
        )

    def start(self):
        print(
            "[MARKET PRODUCER] redis ping =",
            self.redis.ping(),
            flush=True,
        )

        self.symbols = list(
            get_cached_linear_symbols()
        )

        if not self.symbols:
            raise RuntimeError(
                "No cached linear symbols"
            )

        print(
            "[MARKET PRODUCER] symbols =",
            len(self.symbols),
            flush=True,
        )

        self.ws = WebSocket(
            testnet=False,
            channel_type="linear",
        )

        for symbol in self.symbols:
            self.ws.ticker_stream(
                symbol=symbol,
                callback=self._on_ticker,
            )

        print(
            "[MARKET PRODUCER] subscribed =",
            len(self.symbols),
            flush=True,
        )

        while self.running:
            time.sleep(1)

            self._watchdog()

            age = time.time() - self.started_at

            ws_age = (
                None
                if self.last_ws_received_at is None
                else (
                    time.time()
                    - self.last_ws_received_at
                )
            )

            print(
                "[MARKET PRODUCER]",
                "uptime=",
                round(age, 1),
                "received=",
                len(self.received),
                "/",
                len(self.symbols),
                "messages=",
                self.message_count,
                "source=",
                self.source,
                "ws_age=",
                (
                    None
                    if ws_age is None
                    else round(ws_age, 2)
                ),
                "rest_recovery=",
                self.rest_recovery_count,
                flush=True,
            )

    def stop(self):
        self.running = False


producer = MarketWSProducer()


def shutdown(signum, frame):
    print(
        "[MARKET PRODUCER] shutdown",
        flush=True,
    )

    producer.stop()


signal.signal(
    signal.SIGINT,
    shutdown,
)

signal.signal(
    signal.SIGTERM,
    shutdown,
)


if __name__ == "__main__":
    try:
        producer.start()

    except KeyboardInterrupt:
        producer.stop()

    except Exception as exc:
        print(
            "[MARKET PRODUCER ERROR]",
            repr(exc),
            flush=True,
        )

        sys.exit(1)
