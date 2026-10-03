import asyncio
import os
import re

from dotenv import load_dotenv
from telethon import TelegramClient, events


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

        symbol = extract_oi_symbol(text)

        if symbol:
            print()
            print("[OI SIGNAL]")
            print("symbol =", symbol)

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
