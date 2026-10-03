import asyncio
import os
import re

from dotenv import load_dotenv
from telethon import TelegramClient, events

from surge_trading import (
    execute_surge_market_order,
    resolve_surge_symbol_cached,
)


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
                2,
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

            try:
                result = execute_surge_market_order(
                    user_id=2,
                    chat_id=CHAT_ID,
                    message_id=int(event.id),
                    symbol=symbol,
                    side="LONG",
                    dry_run=True,
                )

                print()
                print("[SURGE DRY RUN]")
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
                    "reason     =",
                    result.get("reason"),
                )
                print(
                    "executable =",
                    (
                        result
                        .get("preview", {})
                        .get("executable")
                    ),
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
                    "risk id    =",
                    final_plan.get(
                        "selected_risk_id"
                    ),
                )
                print(
                    "risk limit =",
                    final_plan.get(
                        "selected_risk_limit"
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
                )

            except Exception as exc:
                print()
                print(
                    "[SURGE DRY RUN ERROR]",
                    repr(exc),
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
