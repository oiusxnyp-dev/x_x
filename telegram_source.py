import asyncio
from pathlib import Path
import json
import os
import re
import time

import redis
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram_latency_probe import TelegramLatencyProbe
from surge_message_history import (
    get_previous_text,
    make_webp,
    save_message_version,
)


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

    # 반드시 메시지가 "Oi 급등종목"으로 시작해야 한다.
    # 대소문자와 띄어쓰기 차이는 무시한다.
    if not normalize(text).startswith("oi급등종목"):
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

    EDIT_MAX_AGE_SECONDS = 60

    # 웹 기록 전용 큐.
    # 실매매 handler는 이 큐를 기다리지 않는다.
    history_queue = asyncio.Queue(
        maxsize=1000,
    )

    async def history_worker():
        print(
            "[WEB HISTORY WORKER] STARTED",
            flush=True,
        )

        while True:
            job = await history_queue.get()

            try:
                event = job["event"]

                chat_id = job["chat_id"]
                message_id = job["message_id"]
                chat_name = job["chat_name"]
                event_type = job["event_type"]
                text = job["text"]
                raw_symbol = job["raw_symbol"]
                received_at = job["received_at"]
                telegram_date = job["telegram_date"]
                edit_date = job["edit_date"]

                # 이전 버전 조회도 별도 thread.
                previous_text = None

                if event_type == "EDIT":
                    previous_text = (
                        await asyncio.to_thread(
                            get_previous_text,
                            chat_id,
                            message_id,
                        )
                    )

                # Sender 조회는 Telethon async 작업.
                sender_id = getattr(
                    event,
                    "sender_id",
                    None,
                )

                sender_name = ""
                sender_username = ""

                try:
                    sender = await event.get_sender()

                    if sender is not None:
                        sender_id = (
                            getattr(
                                sender,
                                "id",
                                None,
                            )
                            or sender_id
                        )

                        sender_username = (
                            getattr(
                                sender,
                                "username",
                                None,
                            )
                            or ""
                        )

                        first_name = (
                            getattr(
                                sender,
                                "first_name",
                                None,
                            )
                            or ""
                        )

                        last_name = (
                            getattr(
                                sender,
                                "last_name",
                                None,
                            )
                            or ""
                        )

                        title = (
                            getattr(
                                sender,
                                "title",
                                None,
                            )
                            or ""
                        )

                        sender_name = " ".join(
                            value
                            for value in (
                                first_name,
                                last_name,
                            )
                            if value
                        ).strip()

                        if not sender_name:
                            sender_name = (
                                title
                                or sender_username
                                or ""
                            )

                except Exception as exc:
                    print(
                        "[WEB SENDER WARNING]",
                        "message_id =",
                        message_id,
                        "error =",
                        (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        flush=True,
                    )

                # 실제 sender를 확인하지 못한 경우에는
                # channel name을 sender로 저장하지 않는다.
                # 웹 UI에서만 chat_name을 fallback으로 사용한다.

                # 사진 다운로드도 실매매 handler와 분리.
                media_type = None
                media_path = None
                downloaded_path = None

                try:
                    if event.photo:
                        temp_dir = (
                            Path(__file__)
                            .resolve()
                            .parent
                            / "telegram_downloads"
                        )

                        temp_dir.mkdir(
                            parents=True,
                            exist_ok=True,
                        )

                        media_version = str(
                            int(
                                float(received_at)
                                * 1000
                            )
                        )

                        temp_target = (
                            temp_dir
                            / (
                                "web_"
                                f"{abs(chat_id)}_"
                                f"{message_id}_"
                                f"{media_version}"
                            )
                        )

                        downloaded_path = (
                            await event.download_media(
                                file=str(temp_target),
                            )
                        )

                        if downloaded_path:
                            media_path = (
                                await asyncio.to_thread(
                                    make_webp,
                                    downloaded_path,
                                    chat_id,
                                    message_id,
                                    received_at,
                                )
                            )

                            media_type = "photo"

                except Exception as exc:
                    print(
                        "[WEB MEDIA WARNING]",
                        "message_id =",
                        message_id,
                        "error =",
                        (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        flush=True,
                    )

                # 다운로드한 Telegram 원본은 보존한다.
                # 웹에서는 media_path의 WebP preview만 사용한다.

                # SQLite 저장도 별도 thread.
                await asyncio.to_thread(
                    save_message_version,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    message_id=message_id,
                    event_type=event_type,
                    text=text,
                    previous_text=previous_text,
                    is_surge=bool(raw_symbol),
                    raw_symbol=raw_symbol,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    sender_username=(
                        sender_username
                    ),
                    telegram_date=telegram_date,
                    edit_date=edit_date,
                    received_at=received_at,
                    media_type=media_type,
                    media_path=media_path,
                )

                print(
                    "[WEB HISTORY SAVED]",
                    "message_id =",
                    message_id,
                    "event_type =",
                    event_type,
                    "surge =",
                    bool(raw_symbol),
                    "media =",
                    bool(media_path),
                    flush=True,
                )

            except Exception as exc:
                # 웹 기록 실패는 실매매에 전파하지 않는다.
                print(
                    "[WEB HISTORY WARNING]",
                    (
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    ),
                    flush=True,
                )

            finally:
                history_queue.task_done()

    # 웹 기록 worker는 독립 task.
    asyncio.create_task(
        history_worker()
    )

    latency_probe = TelegramLatencyProbe()

    async def handler(event):
        text = event.raw_text or ""

        event_type = (
            "EDIT"
            if isinstance(
                event,
                events.MessageEdited.Event,
            )
            else "NEW"
        )

        chat_id = int(event.chat_id)
        message_id = int(event.id)

        received_at = time.time()
        raw_to_handler_ms = latency_probe.measure(
            event.message, event_type
        )

        telegram_date = (
            event.date.isoformat()
            if event.date
            else ""
        )

        edit_date = (
            event.edit_date.isoformat()
            if event.edit_date
            else ""
        )

        chat_name = target_names.get(
            chat_id,
            "",
        )

        raw_symbol = extract_oi_symbol(text)

        if raw_symbol:
            reference_date = (
                event.edit_date
                if event_type == 'EDIT' and event.edit_date
                else event.date
            )
            print(
                '[TELEGRAM LATENCY]',
                'message_id =', message_id,
                'event_type =', event_type,
                'raw_to_handler_ms =', raw_to_handler_ms,
                'telegram_to_handler_s =',
                round(received_at - reference_date.timestamp(), 3)
                if reference_date else None,
                flush=True,
            )

        # ========================================================
        # LIVE TRADING HOT PATH
        #
        # 웹 DB / sender / 사진 / Pillow를 기다리지 않는다.
        # ========================================================

        if raw_symbol:
            # 오래 지난 수정본은 실매매로 보내지 않는다.
            allow_trade_event = True

            if event_type == "EDIT":
                message_date_obj = event.date
                edit_date_obj = event.edit_date

                if (
                    message_date_obj
                    and edit_date_obj
                ):
                    edit_age = (
                        edit_date_obj
                        - message_date_obj
                    ).total_seconds()

                    if (
                        edit_age
                        > EDIT_MAX_AGE_SECONDS
                    ):
                        allow_trade_event = False

                        print(
                            "[OI EDIT TRADE SKIP]",
                            "message_id =",
                            message_id,
                            "age_seconds =",
                            edit_age,
                            flush=True,
                        )

            if allow_trade_event:
                payload = {
                    "chat_id": str(chat_id),
                    "chat_name": chat_name,
                    "message_id": str(
                        message_id
                    ),
                    "raw_symbol": raw_symbol,
                    "event_type": event_type,
                    "text": text,
                    "telegram_date": (
                        telegram_date
                    ),
                    "edit_date": edit_date,
                    "received_at": str(
                        received_at
                    ),
                }

                stream_id = (
                    redis_client.xadd(
                        REDIS_STREAM,
                        payload,
                        maxlen=REDIS_MAXLEN,
                        approximate=True,
                    )
                )

                print()
                print("=" * 80)
                print("[OI -> REDIS]")
                print(
                    "event_type =",
                    event_type,
                )
                print(
                    "stream_id =",
                    stream_id,
                )
                print(
                    "chat_id   =",
                    chat_id,
                )
                print(
                    "chat_name =",
                    chat_name,
                )
                print(
                    "message_id =",
                    message_id,
                )
                print(
                    "raw_symbol =",
                    raw_symbol,
                )
                print(
                    "=" * 80,
                    flush=True,
                )

        # ========================================================
        # WEB HISTORY PATH
        #
        # 모든 멍꿀단 메시지.
        # 절대로 queue 완료를 기다리지 않는다.
        # ========================================================

        history_job = {
            "event": event,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "message_id": message_id,
            "event_type": event_type,
            "text": text,
            "raw_symbol": raw_symbol,
            "telegram_date": telegram_date,
            "edit_date": edit_date,
            "received_at": received_at,
        }

        try:
            history_queue.put_nowait(
                history_job
            )

        except asyncio.QueueFull:
            # 웹 기록 누락은 허용한다.
            # 실매매는 이미 위에서 처리됐다.
            print(
                "[WEB HISTORY DROP]",
                "message_id =",
                message_id,
                "reason = QUEUE_FULL",
                flush=True,
            )

    client.add_event_handler(
        latency_probe.raw_handler,
        events.Raw(),
    )

    client.add_event_handler(
        handler,
        events.NewMessage(
            chats=list(target_ids),
        ),
    )

    client.add_event_handler(
        handler,
        events.MessageEdited(
            chats=list(target_ids),
        ),
    )


    # ========================================================
    # WEB-ONLY TELEGRAM HISTORY BACKFILL
    # Never call the live trading handler or Redis XADD.
    # ========================================================
    async def backfill_web_history():
        from datetime import datetime, timezone
        from surge_message_history import (
            DB_PATH,
            save_message_version,
            make_webp,
        )
        import sqlite3

        LIMIT_PER_CHAT = 100

        print(
            "[WEB BACKFILL] START",
            "limit_per_chat =",
            LIMIT_PER_CHAT,
            flush=True,
        )

        for chat_id in sorted(target_ids):
            chat_name = target_names[chat_id]
            inserted = 0
            skipped = 0
            errors = 0

            try:
                async for message in client.iter_messages(
                    chat_id,
                    limit=LIMIT_PER_CHAT,
                ):
                    if not message:
                        continue

                    message_id = int(message.id)
                    text = message.raw_text or ""
                    raw_symbol = extract_oi_symbol(text)

                    # Skip existing history versions.
                    # Never overwrite observed NEW/EDIT events.
                    def already_saved():
                        with sqlite3.connect(DB_PATH) as con:
                            row = con.execute(
                                """
                                SELECT 1
                                FROM surge_message_versions
                                WHERE chat_id = ?
                                  AND message_id = ?
                                LIMIT 1
                                """,
                                (chat_id, message_id),
                            ).fetchone()
                        return row is not None

                    exists = await asyncio.to_thread(
                        already_saved
                    )
                    # Existing rows with missing photos need repair.
                    if exists and not message.photo:
                        skipped += 1
                        continue
                    if exists and message.photo:
                        def has_photo():
                            with sqlite3.connect(DB_PATH) as con:
                                return con.execute(
                                    """
                                    SELECT 1
                                    FROM surge_message_versions
                                    WHERE chat_id = ?
                                      AND message_id = ?
                                      AND media_path IS NOT NULL
                                      AND media_path != ''
                                    LIMIT 1
                                    """,
                                    (chat_id, message_id),
                                ).fetchone() is not None

                        if await asyncio.to_thread(has_photo):
                            skipped += 1
                            continue

                    date_obj = message.date
                    if date_obj is None:
                        skipped += 1
                        continue

                    if date_obj.tzinfo is None:
                        date_obj = date_obj.replace(
                            tzinfo=timezone.utc
                        )

                    original_ts = date_obj.timestamp()

                    sender_id = message.sender_id
                    sender_name = ""
                    sender_username = ""

                    try:
                        sender = await message.get_sender()
                        if sender is not None:
                            sender_id = (
                                getattr(sender, "id", None)
                                or sender_id
                            )
                            sender_username = (
                                getattr(sender, "username", None)
                                or ""
                            )
                            sender_name = " ".join(
                                x for x in (
                                    getattr(sender, "first_name", "") or "",
                                    getattr(sender, "last_name", "") or "",
                                )
                                if x
                            ).strip()
                            if not sender_name:
                                sender_name = (
                                    getattr(sender, "title", None)
                                    or sender_username
                                    or ""
                                )
                    except Exception as exc:
                        print(
                            "[WEB BACKFILL SENDER WARNING]",
                            chat_id,
                            message_id,
                            repr(exc),
                            flush=True,
                        )

                    media_type = None
                    media_path = None

                    try:
                        if message.photo:
                            temp_dir = (
                                Path(__file__).resolve().parent
                                / "telegram_downloads"
                            )
                            temp_dir.mkdir(
                                parents=True,
                                exist_ok=True,
                            )
                            temp_target = (
                                temp_dir
                                / (
                                    f"backfill_{abs(chat_id)}_"
                                    f"{message_id}_{int(original_ts)}"
                                )
                            )
                            downloaded = await message.download_media(
                                file=str(temp_target)
                            )
                            if downloaded:
                                media_path = await asyncio.to_thread(
                                    make_webp,
                                    downloaded,
                                    chat_id,
                                    message_id,
                                    original_ts,
                                )
                                media_type = "photo"
                    except Exception as exc:
                        print(
                            "[WEB BACKFILL MEDIA WARNING]",
                            chat_id,
                            message_id,
                            repr(exc),
                            flush=True,
                        )

                    if exists:
                        if media_path:
                            def repair_photo():
                                with sqlite3.connect(DB_PATH) as con:
                                    cur = con.execute(
                                        """
                                        UPDATE surge_message_versions
                                        SET media_type = ?,
                                            media_path = ?
                                        WHERE chat_id = ?
                                          AND message_id = ?
                                          AND (
                                              media_path IS NULL
                                              OR media_path = ''
                                          )
                                        """,
                                        (
                                            "photo",
                                            media_path,
                                            chat_id,
                                            message_id,
                                        ),
                                    )
                                    con.commit()
                                    return cur.rowcount

                            repaired = await asyncio.to_thread(
                                repair_photo
                            )
                            inserted += repaired
                        else:
                            errors += 1
                        await asyncio.sleep(0.05)
                        continue

                    try:
                        saved = await asyncio.to_thread(
                            save_message_version,
                            chat_id=chat_id,
                            chat_name=chat_name,
                            message_id=message_id,
                            event_type="NEW",
                            text=text,
                            previous_text=None,
                            is_surge=bool(raw_symbol),
                            raw_symbol=raw_symbol,
                            sender_id=sender_id,
                            sender_name=sender_name,
                            sender_username=sender_username,
                            telegram_date=message.date.isoformat(),
                            edit_date=(
                                message.edit_date.isoformat()
                                if message.edit_date
                                else ""
                            ),
                            received_at=original_ts,
                            media_type=media_type,
                            media_path=media_path,
                        )
                        if saved:
                            inserted += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        errors += 1
                        print(
                            "[WEB BACKFILL SAVE WARNING]",
                            chat_id,
                            message_id,
                            repr(exc),
                            flush=True,
                        )

                    # Yield control to live Telegram updates.
                    await asyncio.sleep(0.05)

            except Exception as exc:
                errors += 1
                print(
                    "[WEB BACKFILL CHAT WARNING]",
                    chat_id,
                    repr(exc),
                    flush=True,
                )

            print(
                "[WEB BACKFILL CHAT DONE]",
                "chat_id =", chat_id,
                "inserted =", inserted,
                "skipped =", skipped,
                "errors =", errors,
                flush=True,
            )

        print("[WEB BACKFILL] DONE", flush=True)

    # Event handlers are registered above.
    # Backfill runs independently from live trading.
    asyncio.create_task(backfill_web_history())

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
