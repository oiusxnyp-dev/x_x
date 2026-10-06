import asyncio
import os
import re

from dotenv import load_dotenv
from telethon import TelegramClient, events

from surge_trading import (
    execute_surge_market_order,
    get_effective_settings,
    resolve_surge_symbol_cached,
)
from user_db import get_users


load_dotenv(".env")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

SESSION = "telegram_source"
CHAT_ID = -1004442764226


def normalize(text):
    return "".join((text or "").lower().split())


def extract_oi_symbol(text):
    """
    Oi 급등종목 메시지라면 마지막 해시태그를 반환.
    아니면 None.
    """

    text = text or ""

    # 띄어쓰기 차이는 무시
    if "oi급등종목" not in normalize(text):
        return None

    tags = re.findall(
        r"#\s*([A-Za-z0-9_]+)",
        text,
        flags=re.I,
    )

    if not tags:
        return None

    return tags[-1].upper()


async def main():
    client = TelegramClient(
        SESSION,
        API_ID,
        API_HASH,
    )

    @client.on(events.NewMessage(chats=CHAT_ID))
    async def handler(event):
        text = event.raw_text or ""

        print()
        print("=" * 80)
        print("[TELEGRAM]")
        print("message_id =", event.id)
        print("-" * 80)
        print(text)

        raw_symbol = extract_oi_symbol(text)

        if raw_symbol:
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
                    "[SURGE SKIP] "
                    "symbol could not be resolved",
                    flush=True,
                )
                print("=" * 80, flush=True)
                return

            symbol = resolution["symbol"]

            print(
                "final      =",
                symbol,
            )

            # ------------------------------------------------
            # Surge executor E2E DRY RUN
            #
            # 실제 주문 없음
            # 레버리지 변경 없음
            # execution claim 없음
            #
            # 현재 Oi 급등종목 전략은 LONG 경로만 검증한다.
            # 실거래 전환은 별도 검증 후 한다.
            # ------------------------------------------------

            # ------------------------------------------------
            # Concurrent per-user execution fan-out.
            #
            # One Telegram signal is resolved once above.
            # Every eligible BYBIT user then enters an
            # independent worker concurrently.
            #
            # execute_surge_market_order() is synchronous
            # because it uses the pybit HTTP client, so each
            # user execution is moved to a worker thread with
            # asyncio.to_thread().
            #
            # The executor itself performs the final
            # fail-closed effective-enabled interlock again
            # immediately before real execution.
            # ------------------------------------------------

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

                if not effective.get(
                    "effective_enabled"
                ):
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
                event.id,
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
                        chat_id=CHAT_ID,
                        message_id=int(event.id),
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

                if isinstance(
                    result,
                    BaseException,
                ):
                    print(
                        "error      =",
                        repr(result),
                        flush=True,
                    )
                    continue

                print(
                    "message_id =",
                    event.id,
                )
                print(
                    "symbol     =",
                    result.get("symbol"),
                )
                print(
                    "side       =",
                    result.get("side"),
                )
                print(
                    "order_side =",
                    result.get("order_side"),
                )
                print(
                    "position   =",
                    result.get("position_idx"),
                )
                print(
                    "executed   =",
                    result.get("executed"),
                )
                print(
                    "reason     =",
                    result.get("reason"),
                )

                final_plan = (
                    result
                    .get("preview", {})
                    .get("final_plan", {})
                )

                print(
                    "stage      =",
                    final_plan.get(
                        "signal_stage"
                    ),
                )
                print(
                    "percent    =",
                    final_plan.get(
                        "entry_percent"
                    ),
                )
                print(
                    "leverage   =",
                    final_plan.get(
                        "selected_leverage"
                    ),
                )
                print(
                    "qty        =",
                    final_plan.get("qty"),
                )
                print(
                    "notional   =",
                    final_plan.get(
                        "final_notional"
                    ),
                    flush=True,
                )

        print("=" * 80, flush=True)

    await client.start()

    me = await client.get_me()

    print("=" * 80)
    print("TELEGRAM LISTENER STARTED")
    print("ACCOUNT =", me.username or me.id)
    print("CHAT_ID =", CHAT_ID)
    print("MODE    = READ ONLY")
    print("=" * 80)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
