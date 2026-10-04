import json
import signal
import sys
import time

import redis
from pybit.unified_trading import WebSocket

from surge_trading import get_cached_linear_symbols


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379

KEY_PREFIX = "market:"
META_KEY = "market:meta"

STALE_SECONDS = 5.0


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

    def _write_ticker(
        self,
        symbol,
        data,
    ):
        now = time.time()

        key = KEY_PREFIX + symbol

        # ticker는 delta일 수 있으므로
        # 기존 Redis 값을 유지하면서
        # 이번 message에 들어온 필드만 갱신한다.
        mapping = {
            "symbol": symbol,
            "updated_at": str(now),
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
            mapping={
                "updated_at": str(now),
                "message_count":
                    str(self.message_count),
                "received_symbols":
                    str(len(self.received)),
                "expected_symbols":
                    str(len(self.symbols)),
            },
        )

        pipe.execute()

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

        self.received.add(symbol)
        self.message_count += 1

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

            age = time.time() - self.started_at

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
