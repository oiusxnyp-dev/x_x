import asyncio
import json
import os
import re
import time

import redis
from dotenv import load_dotenv
from telethon import TelegramClient, events


load_dotenv(".env")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

SESSION = "telegram_source"

CHANNEL_KEYWORD = "멍꿀단"

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_STREAM = "surge:telegram:signals"
REDIS_MAXLEN = 10000


def normalize(text):
    return "".join((text or "").lower().split())


def extract_oi_symbol(text):
    """
    Oi 급등종목 메시지에서 마지막 해시태그를 반환.
    아니면 None.
    """
    text = text or ""

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

    client = TelegramClient(
        SESSION,
        API_ID,
        API_HASH,
    )

    await client.start()

    me = await client.get_me()

    target_ids = set()
    target_names = {}

    async for dialog in client.iter_dialogs():
        name = (dialog.name or "").strip()

        if CHANNEL_KEYWORD not in name:
            continue

        kind = type(dialog.entity).__name__

        if kind not in ("Channel", "Chat"):
            continue

        chat_id = int(dialog.id)

        target_ids.add(chat_id)
        target_names[chat_id] = name

    print("=" * 80)
    print("TELEGRAM SOURCE STARTED")
    print("ACCOUNT =", me.username or me.id)
    print("KEYWORD =", CHANNEL_KEYWORD)
    print("STREAM  =", REDIS_STREAM)
    print("TARGETS =", len(target_ids))

    for chat_id in sorted(target_ids):
        print(
            "  ",
            chat_id,
            repr(target_names[chat_id]),
        )

    print("=" * 80, flush=True)

    if not target_ids:
        raise RuntimeError(
            f"No Telegram dialogs matched: "
            f"{CHANNEL_KEYWORD!r}"
        )

    @client.on(
        events.NewMessage(
            chats=list(target_ids),
        )
    )
    async def handler(event):
        text = event.raw_text or ""

        raw_symbol = extract_oi_symbol(text)

        # 급등종목이 아니면 Redis로 보내지 않는다.
        if not raw_symbol:
            return

        chat_id = int(event.chat_id)
        message_id = int(event.id)

        payload = {
            "chat_id": str(chat_id),
            "chat_name": target_names.get(
                chat_id,
                "",
            ),
            "message_id": str(message_id),
            "raw_symbol": raw_symbol,
            "text": text,
            "telegram_date": (
                event.date.isoformat()
                if event.date
                else ""
            ),
            "received_at": str(time.time()),
        }

        stream_id = redis_client.xadd(
            REDIS_STREAM,
            payload,
            maxlen=REDIS_MAXLEN,
            approximate=True,
        )

        print()
        print("=" * 80)
        print("[OI -> REDIS]")
        print("stream_id =", stream_id)
        print("chat_id   =", chat_id)
        print(
            "chat_name =",
            target_names.get(chat_id, ""),
        )
        print("message_id =", message_id)
        print("raw_symbol =", raw_symbol)
        print("=" * 80, flush=True)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
