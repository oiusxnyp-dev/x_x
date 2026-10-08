import sqlite3
import time
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageOps


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "surge_message_history.db"

MEDIA_DIR = BASE_DIR / "surge_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

WEBP_MAX_SIDE = 960
WEBP_QUALITY = 80


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_message_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_name TEXT,
                message_id INTEGER NOT NULL,

                event_type TEXT NOT NULL,
                text TEXT NOT NULL,
                previous_text TEXT,

                is_surge INTEGER NOT NULL DEFAULT 0,
                raw_symbol TEXT,

                sender_id INTEGER,
                sender_name TEXT,
                sender_username TEXT,

                telegram_date TEXT,
                edit_date TEXT,
                received_at REAL NOT NULL,

                media_type TEXT,
                media_path TEXT,

                UNIQUE (
                    chat_id,
                    message_id,
                    event_type,
                    text
                )
            )
        """)

        columns = {
            row[1]
            for row in con.execute(
                "PRAGMA table_info(surge_message_versions)"
            )
        }

        additions = {
            "previous_text": "TEXT",
            "is_surge": "INTEGER NOT NULL DEFAULT 0",
            "sender_id": "INTEGER",
            "sender_name": "TEXT",
            "sender_username": "TEXT",
            "media_type": "TEXT",
            "media_path": "TEXT",
        }

        for name, definition in additions.items():
            if name not in columns:
                con.execute(
                    f"ALTER TABLE surge_message_versions "
                    f"ADD COLUMN {name} {definition}"
                )

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_surge_message_versions_message
            ON surge_message_versions (
                chat_id,
                message_id,
                received_at
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_surge_message_versions_received
            ON surge_message_versions (
                received_at
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_surge_message_versions_surge
            ON surge_message_versions (
                is_surge,
                received_at
            )
        """)


def get_previous_text(chat_id, message_id):
    init_db()

    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute("""
            SELECT text
            FROM surge_message_versions
            WHERE chat_id = ?
              AND message_id = ?
            ORDER BY received_at DESC, id DESC
            LIMIT 1
        """, (
            int(chat_id),
            int(message_id),
        )).fetchone()

    if not row:
        return None

    return row[0]


def save_message_version(
    *,
    chat_id,
    chat_name,
    message_id,
    event_type,
    text,
    previous_text=None,
    is_surge=False,
    raw_symbol=None,
    sender_id=None,
    sender_name=None,
    sender_username=None,
    telegram_date=None,
    edit_date=None,
    received_at=None,
    media_type=None,
    media_path=None,
):
    init_db()

    received_at = (
        float(received_at)
        if received_at is not None
        else time.time()
    )

    with closing(sqlite3.connect(DB_PATH)) as con, con:
        cur = con.execute("""
            INSERT OR IGNORE INTO surge_message_versions (
                chat_id,
                chat_name,
                message_id,
                event_type,
                text,
                previous_text,
                is_surge,
                raw_symbol,
                sender_id,
                sender_name,
                sender_username,
                telegram_date,
                edit_date,
                received_at,
                media_type,
                media_path
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
        """, (
            int(chat_id),
            str(chat_name or ""),
            int(message_id),
            str(event_type),
            str(text or ""),
            previous_text,
            1 if is_surge else 0,
            str(raw_symbol or ""),
            int(sender_id) if sender_id is not None else None,
            str(sender_name or ""),
            str(sender_username or ""),
            telegram_date,
            edit_date,
            received_at,
            media_type,
            media_path,
        ))

        return cur.rowcount == 1


def make_webp(
    source_path,
    chat_id,
    message_id,
    received_at=None,
):
    source_path = Path(source_path)

    if received_at is None:
        version_token = "0"
    else:
        version_token = str(
            int(float(received_at) * 1000)
        )

    target_name = (
        f"{abs(int(chat_id))}_"
        f"{int(message_id)}_"
        f"{version_token}.webp"
    )

    target_path = MEDIA_DIR / target_name

    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image)

        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")

        image.thumbnail(
            (
                WEBP_MAX_SIDE,
                WEBP_MAX_SIDE,
            )
        )

        image.save(
            target_path,
            "WEBP",
            quality=WEBP_QUALITY,
            method=4,
        )

    return (
        "/api/surge/media/"
        + target_name
    )


init_db()


# ============================================================
# Web feed reader
# ============================================================

LEGACY_LIVE_DB = (
    Path.home()
    / "telegram_reader"
    / "telegram_raw.db"
)

LEGACY_ARCHIVE_DB = (
    Path.home()
    / "telegram_reader"
    / "telegram_archive"
    / "telegram_raw.db"
)

MUNGGUL_CHAT_IDS = (
    -1004442764226,
    -1003849484550,
)


def _looks_like_surge(text):
    compact = "".join(
        (text or "").lower().split()
    )

    return compact.startswith(
        "oi급등종목"
    )


def _history_rows():
    if not DB_PATH.exists():
        return []

    with closing(
        sqlite3.connect(
            f"file:{DB_PATH}?mode=ro",
            uri=True,
        )
    ) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT *
            FROM surge_message_versions
            ORDER BY received_at
        """).fetchall()

    result = []

    for row in rows:
        data = dict(row)

        result.append({
            "version_id": int(data["id"]),
            "chat_id": int(data["chat_id"]),
            "chat_name":
                data.get("chat_name") or "",
            "message_id":
                int(data["message_id"]),
            "event_type":
                data.get("event_type") or "NEW",
            "text":
                data.get("text") or "",
            "previous_text":
                data.get("previous_text"),
            "is_surge":
                bool(data.get("is_surge")),
            "raw_symbol":
                data.get("raw_symbol") or "",
            "sender_id":
                data.get("sender_id"),
            "sender_name":
                data.get("sender_name") or "",
            "sender_username":
                data.get("sender_username") or "",
            "message_time_iso":
                data.get("telegram_date"),
            "edit_time_iso":
                data.get("edit_date"),
            "received_at":
                data.get("received_at"),
            "media_type":
                data.get("media_type"),
            "media_path":
                data.get("media_path"),
            "source": "history",
        })

    return result


def _legacy_live_rows():
    if not LEGACY_LIVE_DB.exists():
        return []

    with closing(
        sqlite3.connect(
            f"file:{LEGACY_LIVE_DB}?mode=ro",
            uri=True,
        )
    ) as con:
        con.row_factory = sqlite3.Row

        placeholders = ",".join(
            "?"
            for _ in MUNGGUL_CHAT_IDS
        )

        rows = con.execute(
            f"""
            SELECT
                chat_id,
                message_id,
                sender_id,
                sender_name,
                chat_name,
                text,
                message_time_iso,
                edit_time_iso,
                first_seen_at,
                is_deleted
            FROM telegram_messages
            WHERE chat_id IN ({placeholders})
              AND is_deleted = 0
            ORDER BY message_time, message_id
            """,
            MUNGGUL_CHAT_IDS,
        ).fetchall()

    result = []

    for row in rows:
        data = dict(row)
        text = data.get("text") or ""

        result.append({
            "chat_id": int(data["chat_id"]),
            "chat_name":
                data.get("chat_name") or "",
            "message_id":
                int(data["message_id"]),
            "event_type":
                (
                    "EDIT"
                    if data.get("edit_time_iso")
                    else "NEW"
                ),
            "text": text,
            "previous_text": None,
            "is_surge":
                _looks_like_surge(text),
            "raw_symbol": "",
            "sender_id":
                data.get("sender_id"),
            "sender_name":
                data.get("sender_name") or "",
            "sender_username": "",
            "message_time_iso":
                data.get("message_time_iso"),
            "edit_time_iso":
                data.get("edit_time_iso"),
            "received_at":
                data.get("first_seen_at"),
            "media_type": None,
            "media_path": None,
            "source": "legacy_live",
        })

    return result


def _legacy_archive_rows():
    if not LEGACY_ARCHIVE_DB.exists():
        return []

    with closing(
        sqlite3.connect(
            f"file:{LEGACY_ARCHIVE_DB}?mode=ro",
            uri=True,
        )
    ) as con:
        con.row_factory = sqlite3.Row

        placeholders = ",".join(
            "?"
            for _ in MUNGGUL_CHAT_IDS
        )

        rows = con.execute(
            f"""
            SELECT
                chat_id,
                message_id,
                date_utc,
                edit_date_utc,
                sender_id,
                post_author,
                grouped_id,
                media_type,
                has_media,
                text
            FROM messages
            WHERE chat_id IN ({placeholders})
            ORDER BY date_utc, message_id
            """,
            MUNGGUL_CHAT_IDS,
        ).fetchall()

    result = []

    for row in rows:
        data = dict(row)
        text = data.get("text") or ""

        result.append({
            "chat_id": int(data["chat_id"]),
            "chat_name": "",
            "message_id":
                int(data["message_id"]),
            "event_type":
                (
                    "EDIT"
                    if data.get("edit_date_utc")
                    else "NEW"
                ),
            "text": text,
            "previous_text": None,
            "is_surge":
                _looks_like_surge(text),
            "raw_symbol": "",
            "sender_id":
                data.get("sender_id"),
            "sender_name":
                data.get("post_author") or "",
            "sender_username": "",
            "message_time_iso":
                data.get("date_utc"),
            "edit_time_iso":
                data.get("edit_date_utc"),
            "received_at": None,
            "media_type":
                data.get("media_type"),
            "media_path": None,
            "grouped_id":
                data.get("grouped_id"),
            "has_media":
                bool(data.get("has_media")),
            "source": "legacy_archive",
        })

    return result


def _event_time(row):
    if row.get("source") == "history":
        if row.get("event_type") == "EDIT":
            return (
                row.get("edit_time_iso")
                or row.get("message_time_iso")
                or ""
            )

    return (
        row.get("message_time_iso")
        or ""
    )


def _event_key(row):
    source = row.get("source") or ""
    event_type = row.get("event_type") or "NEW"

    if source == "history":
        version_id = row.get("version_id")

        return (
            "history",
            int(version_id or 0),
        )

    return (
        source,
        int(row["chat_id"]),
        int(row["message_id"]),
    )


def _cursor_for(row):
    event_time = _event_time(row)

    return "|".join([
        event_time,
        str(int(row["chat_id"])),
        str(int(row["message_id"])),
        str(row.get("source") or ""),
        str(row.get("version_id") or 0),
    ])


def _cursor_key(row):
    return (
        _event_time(row),
        int(row["chat_id"]),
        int(row["message_id"]),
        str(row.get("source") or ""),
        int(row.get("version_id") or 0),
    )


def get_message_feed(
    *,
    limit=12,
    before=None,
):
    limit = max(
        1,
        min(int(limit), 50),
    )

    archive_rows = _legacy_archive_rows()
    live_rows = _legacy_live_rows()
    history_rows = _history_rows()

    # --------------------------------------------------------
    # 기존 DB
    #
    # archive/live가 같은 Telegram message_id를 가지고 있으면
    # 더 최신 source인 live를 사용한다.
    #
    # 과거 DB에는 수정 전 본문이 없으므로 EDIT 이벤트를
    # 인위적으로 만들어내지 않는다.
    # --------------------------------------------------------
    legacy = {}

    for row in archive_rows:
        key = (
            int(row["chat_id"]),
            int(row["message_id"]),
        )

        row = dict(row)
        row["legacy_was_edited"] = bool(
            row.get("edit_time_iso")
        )
        row["event_type"] = "NEW"

        legacy[key] = row

    for row in live_rows:
        key = (
            int(row["chat_id"]),
            int(row["message_id"]),
        )

        previous = legacy.get(key)

        row = dict(row)
        row["legacy_was_edited"] = bool(
            row.get("edit_time_iso")
        )
        row["event_type"] = "NEW"

        # archive에만 media 정보가 있으면 유지.
        if previous:
            if (
                not row.get("media_type")
                and previous.get("media_type")
            ):
                row["media_type"] = (
                    previous.get("media_type")
                )

            if (
                not row.get("media_path")
                and previous.get("media_path")
            ):
                row["media_path"] = (
                    previous.get("media_path")
                )

            if (
                not row.get("grouped_id")
                and previous.get("grouped_id")
            ):
                row["grouped_id"] = (
                    previous.get("grouped_id")
                )

            if (
                not row.get("has_media")
                and previous.get("has_media")
            ):
                row["has_media"] = (
                    previous.get("has_media")
                )

        legacy[key] = row

    # --------------------------------------------------------
    # 새 history DB
    #
    # 같은 message_id라도 NEW / EDIT를 절대로 합치지 않는다.
    # 각 DB row가 웹 피드의 독립 이벤트다.
    #
    # history가 존재하는 message_id의 legacy 최종본은 제외한다.
    # 그렇지 않으면 같은 Telegram 메시지가
    # legacy + NEW/EDIT로 중복 표시된다.
    # --------------------------------------------------------
    history_message_keys = {
        (
            int(row["chat_id"]),
            int(row["message_id"]),
        )
        for row in history_rows
    }

    events = [
        row
        for key, row in legacy.items()
        if key not in history_message_keys
    ]

    events.extend(history_rows)

    events.sort(
        key=_cursor_key,
        reverse=True,
    )

    if before:
        try:
            parts = before.split("|")

            if len(parts) != 5:
                raise ValueError(
                    "invalid cursor"
                )

            before_key = (
                parts[0],
                int(parts[1]),
                int(parts[2]),
                parts[3],
                int(parts[4]),
            )

            events = [
                row
                for row in events
                if _cursor_key(row) < before_key
            ]

        except Exception:
            # 잘못된 cursor는 첫 페이지처럼 처리.
            pass

    page = events[:limit]

    next_cursor = None

    if len(events) > limit and page:
        next_cursor = _cursor_for(
            page[-1]
        )

    return {
        "messages": page,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


def get_latest_oi_surge():
    history = _history_rows()
    history_keys = {
        (int(r["chat_id"]), int(r["message_id"]))
        for r in history
    }

    legacy = {}
    for row in _legacy_archive_rows() + _legacy_live_rows():
        key = (int(row["chat_id"]), int(row["message_id"]))
        if key not in history_keys:
            legacy[key] = row

    rows = history + list(legacy.values())

    matched = [
        r for r in rows
        if _looks_like_surge(r.get("text") or "")
    ]

    if not matched:
        return None

    newest = max(
        matched,
        key=lambda r: (
            r.get("message_time_iso") or "",
            int(r["chat_id"]),
            int(r["message_id"]),
        ),
    )

    key = (int(newest["chat_id"]), int(newest["message_id"]))

    versions = [
        r for r in rows
        if (int(r["chat_id"]), int(r["message_id"])) == key
    ]

    result = dict(max(versions, key=_cursor_key))

    if result.get("source") != "history":
        result["legacy_was_edited"] = bool(
            result.get("edit_time_iso")
        )
        result["event_type"] = "NEW"

    return result
