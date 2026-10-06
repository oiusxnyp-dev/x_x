import asyncio

import redis

from surge_trading import (
    execute_surge_market_order,
    get_effective_settings,
    resolve_surge_symbol_cached,
)
from user_db import get_users


REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_STREAM = "surge:telegram:signals"


async def process_signal(fields):
    chat_id = int(fields["chat_id"])
    message_id = int(fields["message_id"])
    raw_symbol = (fields.get("raw_symbol") or "").strip().upper()
    text = fields.get("text") or ""
    chat_name = fields.get("chat_name") or ""

    print()
    print("=" * 80)
    print("[REDIS TELEGRAM SIGNAL]")
    print("chat_id    =", chat_id)
    print("chat_name  =", chat_name)
    print("message_id =", message_id)
    print("-" * 80)
    print(text)

    if not raw_symbol:
        print("[SURGE SKIP] empty raw_symbol", flush=True)
        print("=" * 80, flush=True)
        return

    print()
    print("[OI SIGNAL]")
    print("raw symbol =", raw_symbol)

    resolution = resolve_surge_symbol_cached(
        raw_symbol,
    )

    print(
        "resolved   =",
        resolution.get("resolved"),
    )
    print(
        "mode       =",
        resolution.get("mode"),
    )
    print(
        "candidates =",
        resolution.get("candidates"),
    )
    print(
        "refreshed  =",
        resolution.get("refreshed"),
    )

    if not resolution.get("resolved"):
        print(
            "[SURGE SKIP] symbol could not be resolved",
            flush=True,
        )
        print("=" * 80, flush=True)
        return

    symbol = resolution["symbol"]

    print("final      =", symbol)

    target_users = []

    for user in get_users():
        uid = int(user["id"])

        if not int(user["approved"]):
            continue

        if not int(user["enabled"]):
            continue

        if str(
            user["exchange"] or ""
        ).upper() != "BYBIT":
            continue

        try:
            effective = get_effective_settings(uid)
        except Exception as exc:
            print(
                "[SURGE USER SKIP]",
                "user_id =",
                uid,
                "reason = EFFECTIVE_SETTINGS_ERROR",
                "error =",
                repr(exc),
                flush=True,
            )
            continue

        if not effective.get("effective_enabled"):
            print(
                "[SURGE USER SKIP]",
                "user_id =",
                uid,
                "reason = AUTO_TRADING_DISABLED",
                flush=True,
            )
            continue

        target_users.append(uid)

    print()
    print(
        "[SURGE FANOUT]",
        "message_id =",
        message_id,
        "symbol =",
        symbol,
        "users =",
        target_users,
        flush=True,
    )

    async def execute_for_user(uid):
        try:
            return await asyncio.to_thread(
                execute_surge_market_order,
                user_id=uid,
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side="LONG",
                dry_run=False,
            )
        except Exception as exc:
            return {
                "ok": False,
                "dry_run": False,
                "executed": False,
                "reason": "USER_EXECUTION_EXCEPTION",
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }

    tasks = [
        execute_for_user(uid)
        for uid in target_users
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True,
    )

    for uid, result in zip(
        target_users,
        results,
    ):
        print()
        print(
            "[SURGE USER RESULT]",
            "user_id =",
            uid,
            flush=True,
        )

        if isinstance(result, BaseException):
            print(
                "error      =",
                repr(result),
                flush=True,
            )
            continue

        print("message_id =", message_id)
        print("symbol     =", result.get("symbol"))
        print("side       =", result.get("side"))
        print("order_side =", result.get("order_side"))
        print("position   =", result.get("position_idx"))
        print("executed   =", result.get("executed"))
        print("reason     =", result.get("reason"))

        final_plan = (
            result
            .get("preview", {})
            .get("final_plan", {})
        )

        print(
            "stage      =",
            final_plan.get("signal_stage"),
        )
        print(
            "percent    =",
            final_plan.get("entry_percent"),
        )
        print(
            "leverage   =",
            final_plan.get("selected_leverage"),
        )
        print(
            "qty        =",
            final_plan.get("qty"),
        )
        print(
            "notional   =",
            final_plan.get("final_notional"),
            flush=True,
        )

    print("=" * 80, flush=True)


async def main():
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        decode_responses=True,
    )

    print(
        "[REDIS] ping =",
        redis_client.ping(),
        flush=True,
    )

    # Start from the current end of the stream.
    # Signals that existed before this process started are NOT executed.
    last_id = "$"

    print("=" * 80)
    print("SURGE REDIS LISTENER STARTED")
    print("STREAM  =", REDIS_STREAM)
    print("START   = NEW EVENTS ONLY")
    print("MODE    = LIVE TRADING")
    print("=" * 80, flush=True)

    while True:
        try:
            rows = await asyncio.to_thread(
                redis_client.xread,
                {REDIS_STREAM: last_id},
                1,
                5000,
            )
        except Exception as exc:
            print(
                "[REDIS READ ERROR]",
                repr(exc),
                flush=True,
            )
            await asyncio.sleep(1)
            continue

        if not rows:
            continue

        for _, messages in rows:
            for stream_id, fields in messages:
                # Advance before execution. The trading executor's
                # claim is the second duplicate-execution guard.
                last_id = stream_id

                print(
                    "[REDIS RECEIVE]",
                    "stream_id =",
                    stream_id,
                    flush=True,
                )

                try:
                    await process_signal(fields)
                except Exception as exc:
                    print(
                        "[SURGE SIGNAL ERROR]",
                        "stream_id =",
                        stream_id,
                        "error =",
                        repr(exc),
                        flush=True,
                    )


if __name__ == "__main__":
    asyncio.run(main())
