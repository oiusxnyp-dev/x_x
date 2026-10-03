import sqlite3
import re
from pathlib import Path
from collections import defaultdict


BASE_DIR = Path(__file__).resolve().parent

SETTINGS_DB = BASE_DIR / "surge_trading.db"

TELEGRAM_READER_DIR = Path.home() / "telegram_reader"
TELEGRAM_USERS_DB = TELEGRAM_READER_DIR / "users.db"
TELEGRAM_RAW_DB = TELEGRAM_READER_DIR / "telegram_raw.db"

MENGGUL_CHAT_ID = -1004442764226


def init_db():
    with sqlite3.connect(SETTINGS_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_settings (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                entry_percent REAL NOT NULL DEFAULT 100.0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()


def get_settings(user_id: int):
    init_db()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.row_factory = sqlite3.Row

        row = con.execute("""
            SELECT
                user_id,
                enabled,
                entry_percent,
                updated_at
            FROM surge_settings
            WHERE user_id = ?
        """, (int(user_id),)).fetchone()

    if row is None:
        return {
            "user_id": int(user_id),
            "enabled": False,
            "entry_percent": 100.0,
            "updated_at": None,
        }

    return {
        "user_id": int(row["user_id"]),
        "enabled": bool(row["enabled"]),
        "entry_percent": float(row["entry_percent"]),
        "updated_at": row["updated_at"],
    }


def telegram_reader_trading_enabled(user_id: int) -> bool:
    if not TELEGRAM_USERS_DB.exists():
        raise RuntimeError(
            f"Telegram Reader users DB not found: {TELEGRAM_USERS_DB}"
        )

    uri = f"file:{TELEGRAM_USERS_DB}?mode=ro"

    with sqlite3.connect(uri, uri=True) as con:
        row = con.execute("""
            SELECT trading_enabled
            FROM users
            WHERE id = ?
        """, (int(user_id),)).fetchone()

    if row is None:
        raise RuntimeError(
            f"Telegram Reader user not found: user_id={user_id}"
        )

    return bool(int(row[0] or 0))


def get_effective_settings(user_id: int):
    settings = get_settings(user_id)

    try:
        telegram_auto = telegram_reader_trading_enabled(user_id)
        interlock_error = None
    except Exception as exc:
        # 원본 상태를 확인할 수 없으면 안전하게 급등매매를 차단한다.
        telegram_auto = None
        interlock_error = str(exc)

    blocked = telegram_auto is True or interlock_error is not None

    return {
        **settings,
        "telegram_reader_trading_enabled": telegram_auto,
        "interlock_error": interlock_error,
        "blocked": blocked,
        "effective_enabled": (
            bool(settings["enabled"])
            and not blocked
        ),
    }


def save_settings(
    user_id: int,
    *,
    enabled: bool,
    entry_percent: float,
):
    entry_percent = float(entry_percent)

    if entry_percent < 0 or entry_percent > 1000:
        raise ValueError(
            "entry_percent must be between 0 and 1000"
        )

    # ON 요청일 때 Telegram Reader 상태를 반드시 원본 DB에서 검사.
    if enabled:
        if telegram_reader_trading_enabled(user_id):
            raise RuntimeError(
                "telegram_reader_trading_enabled"
            )

    init_db()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.execute("""
            INSERT INTO surge_settings (
                user_id,
                enabled,
                entry_percent,
                updated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)

            ON CONFLICT(user_id)
            DO UPDATE SET
                enabled = excluded.enabled,
                entry_percent = excluded.entry_percent,
                updated_at = CURRENT_TIMESTAMP
        """, (
            int(user_id),
            1 if enabled else 0,
            entry_percent,
        ))

        con.commit()


def force_disable(user_id: int):
    init_db()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.execute("""
            UPDATE surge_settings
            SET
                enabled = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (int(user_id),))

        con.commit()


def get_recent_surge_messages(limit: int = 20):
    if not TELEGRAM_RAW_DB.exists():
        raise RuntimeError(
            f"Telegram raw DB not found: {TELEGRAM_RAW_DB}"
        )

    uri = f"file:{TELEGRAM_RAW_DB}?mode=ro"

    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT
                chat_id,
                message_id,
                chat_name,
                text,
                message_time_iso,
                edit_time_iso
            FROM telegram_messages
            WHERE chat_id = ?
              AND (
                    text LIKE '%OI 급등종목%'
                 OR text LIKE '%Oi 급등종목%'
                 OR text LIKE '%oi 급등종목%'
              )
              AND is_deleted = 0
            ORDER BY message_time DESC
            LIMIT ?
        """, (
            MENGGUL_CHAT_ID,
            int(limit),
        )).fetchall()

    return [dict(row) for row in rows]


init_db()


# ============================================================
# Signal-stage entry settings
# ============================================================

DEFAULT_STAGE_PERCENTS = {
    1: 100.0,
    2: 100.0,
    3: 100.0,
    4: 0.0,
    5: 0.0,   # 5차 이후 기본값
}


def init_stage_tables():
    with sqlite3.connect(SETTINGS_DB) as con:
        # 유저별 신호 단계 비중
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_stage_settings (
                user_id INTEGER NOT NULL,
                stage INTEGER NOT NULL,
                entry_percent REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, stage)
            )
        """)

        # 급등매매를 켠 이후 종목별 진행 상태
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_symbol_cycles (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                signal_stage INTEGER NOT NULL DEFAULT 0,
                last_message_id INTEGER,
                last_signal_time TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, symbol)
            )
        """)

        # 실제로 어떤 Telegram 신호를 처리했는지 기록.
        # 서버 재시작/중복 이벤트에서도 같은 신호를 다시 단계 증가시키지 않는다.
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_signal_events (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                signal_stage INTEGER NOT NULL,
                configured_percent REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id, message_id, symbol)
            )
        """)

        con.commit()


def ensure_default_stage_settings(user_id: int):
    init_stage_tables()

    with sqlite3.connect(SETTINGS_DB) as con:
        for stage, percent in DEFAULT_STAGE_PERCENTS.items():
            con.execute("""
                INSERT OR IGNORE INTO surge_stage_settings (
                    user_id,
                    stage,
                    entry_percent
                )
                VALUES (?, ?, ?)
            """, (
                int(user_id),
                int(stage),
                float(percent),
            ))

        con.commit()


def get_stage_settings(user_id: int):
    ensure_default_stage_settings(user_id)

    with sqlite3.connect(SETTINGS_DB) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT
                stage,
                entry_percent
            FROM surge_stage_settings
            WHERE user_id = ?
            ORDER BY stage
        """, (int(user_id),)).fetchall()

    return [
        {
            "stage": int(row["stage"]),
            "entry_percent": float(row["entry_percent"]),
        }
        for row in rows
    ]


def set_stage_percent(
    user_id: int,
    stage: int,
    entry_percent: float,
):
    stage = int(stage)
    entry_percent = float(entry_percent)

    if stage < 1:
        raise ValueError("stage must be >= 1")

    if entry_percent < 0 or entry_percent > 1000:
        raise ValueError(
            "entry_percent must be between 0 and 1000"
        )

    init_stage_tables()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.execute("""
            INSERT INTO surge_stage_settings (
                user_id,
                stage,
                entry_percent,
                updated_at
            )
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)

            ON CONFLICT(user_id, stage)
            DO UPDATE SET
                entry_percent = excluded.entry_percent,
                updated_at = CURRENT_TIMESTAMP
        """, (
            int(user_id),
            stage,
            entry_percent,
        ))

        con.commit()


def get_percent_for_stage(
    user_id: int,
    signal_stage: int,
) -> float:
    """
    signal_stage가 설정된 마지막 stage보다 크면
    마지막 stage 값을 '이후' 값으로 사용한다.

    예:
      stage 5 = 50%
      6차, 7차, 8차... 모두 50%
    """
    settings = get_stage_settings(user_id)

    if not settings:
        return 0.0

    signal_stage = max(1, int(signal_stage))

    exact = next(
        (
            row
            for row in settings
            if row["stage"] == signal_stage
        ),
        None,
    )

    if exact:
        return float(exact["entry_percent"])

    return float(settings[-1]["entry_percent"])


def get_symbol_cycle(user_id: int, symbol: str):
    init_stage_tables()

    symbol = str(symbol).upper().strip()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.row_factory = sqlite3.Row

        row = con.execute("""
            SELECT
                signal_stage,
                last_message_id,
                last_signal_time
            FROM surge_symbol_cycles
            WHERE user_id = ?
              AND symbol = ?
        """, (
            int(user_id),
            symbol,
        )).fetchone()

    if row is None:
        return {
            "symbol": symbol,
            "signal_stage": 0,
            "last_message_id": None,
            "last_signal_time": None,
        }

    return {
        "symbol": symbol,
        "signal_stage": int(row["signal_stage"]),
        "last_message_id": row["last_message_id"],
        "last_signal_time": row["last_signal_time"],
    }


def register_signal_stage(
    user_id: int,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    signal_time: str | None = None,
):
    """
    신규 Telegram message_id에 대해서만 다음 진입단계를 배정한다.

    EDIT는 같은 message_id이므로 단계 증가 없음.
    같은 메시지가 재처리되어도 단계 증가 없음.
    """
    init_stage_tables()

    user_id = int(user_id)
    chat_id = int(chat_id)
    message_id = int(message_id)
    symbol = str(symbol).upper().strip()

    if not symbol:
        raise ValueError("symbol is empty")

    with sqlite3.connect(SETTINGS_DB) as con:
        con.row_factory = sqlite3.Row

        con.execute("BEGIN IMMEDIATE")

        existing = con.execute("""
            SELECT
                signal_stage,
                configured_percent
            FROM surge_signal_events
            WHERE user_id = ?
              AND chat_id = ?
              AND message_id = ?
              AND symbol = ?
        """, (
            user_id,
            chat_id,
            message_id,
            symbol,
        )).fetchone()

        if existing is not None:
            con.commit()

            return {
                "new": False,
                "symbol": symbol,
                "signal_stage": int(
                    existing["signal_stage"]
                ),
                "entry_percent": float(
                    existing["configured_percent"]
                ),
            }

        cycle = con.execute("""
            SELECT signal_stage
            FROM surge_symbol_cycles
            WHERE user_id = ?
              AND symbol = ?
        """, (
            user_id,
            symbol,
        )).fetchone()

        previous_stage = (
            int(cycle["signal_stage"])
            if cycle is not None
            else 0
        )

        signal_stage = previous_stage + 1

        # 현재 유저의 단계별 비중 조회
        rows = con.execute("""
            SELECT stage, entry_percent
            FROM surge_stage_settings
            WHERE user_id = ?
            ORDER BY stage
        """, (user_id,)).fetchall()

        if not rows:
            configured_percent = float(
                DEFAULT_STAGE_PERCENTS.get(
                    signal_stage,
                    DEFAULT_STAGE_PERCENTS[
                        max(DEFAULT_STAGE_PERCENTS)
                    ],
                )
            )
        else:
            exact = next(
                (
                    row
                    for row in rows
                    if int(row["stage"]) == signal_stage
                ),
                None,
            )

            if exact is not None:
                configured_percent = float(
                    exact["entry_percent"]
                )
            else:
                configured_percent = float(
                    rows[-1]["entry_percent"]
                )

        con.execute("""
            INSERT INTO surge_signal_events (
                user_id,
                chat_id,
                message_id,
                symbol,
                signal_stage,
                configured_percent
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            chat_id,
            message_id,
            symbol,
            signal_stage,
            configured_percent,
        ))

        con.execute("""
            INSERT INTO surge_symbol_cycles (
                user_id,
                symbol,
                signal_stage,
                last_message_id,
                last_signal_time,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)

            ON CONFLICT(user_id, symbol)
            DO UPDATE SET
                signal_stage = excluded.signal_stage,
                last_message_id = excluded.last_message_id,
                last_signal_time = excluded.last_signal_time,
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id,
            symbol,
            signal_stage,
            message_id,
            signal_time,
        ))

        con.commit()

    return {
        "new": True,
        "symbol": symbol,
        "signal_stage": signal_stage,
        "entry_percent": configured_percent,
    }


def reset_symbol_cycle(user_id: int, symbol: str):
    """
    이후 필요할 수 있는 수동 사이클 초기화용.
    과거 이벤트 감사기록은 삭제하지 않는다.
    """
    init_stage_tables()

    symbol = str(symbol).upper().strip()

    with sqlite3.connect(SETTINGS_DB) as con:
        con.execute("""
            DELETE FROM surge_symbol_cycles
            WHERE user_id = ?
              AND symbol = ?
        """, (
            int(user_id),
            symbol,
        ))

        con.commit()


init_stage_tables()




# ---------------------------------------------------------------------------
# Historical Menggul surge-signal reader
# ---------------------------------------------------------------------------

SURGE_ARCHIVE_DB = (
    TELEGRAM_READER_DIR
    / "telegram_archive"
    / "telegram_raw.db"
)

SURGE_SIGNAL_RE = re.compile(
    r"(?i)\boi\s*급등종목"
)

SURGE_TAG_RE = re.compile(
    r"#([A-Za-z0-9]+)"
)

SURGE_SYMBOL_ALIASES = {
    "MUABARAK": "MUBARAK",
}


def normalize_surge_symbol(symbol: str | None):
    if not symbol:
        return None

    symbol = str(symbol).upper().strip()

    symbol = SURGE_SYMBOL_ALIASES.get(
        symbol,
        symbol,
    )

    # '#OI급등종목' 같은 공지성 태그는
    # 실제 거래 종목으로 취급하지 않는다.
    if symbol == "OI":
        return None

    return symbol


def _read_historical_surge_rows():
    """
    과거 archive의 messages 테이블을 읽는다.
    """
    if not SURGE_ARCHIVE_DB.exists():
        return []

    uri = f"file:{SURGE_ARCHIVE_DB}?mode=ro"

    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT
                message_id,
                date_utc,
                text
            FROM messages
            WHERE chat_id = ?
              AND text IS NOT NULL
            ORDER BY date_utc, message_id
        """, (
            MENGGUL_CHAT_ID,
        )).fetchall()

    result = []

    for row in rows:
        text = row["text"] or ""

        if not SURGE_SIGNAL_RE.search(text):
            continue

        result.append({
            "chat_id": MENGGUL_CHAT_ID,
            "message_id": int(row["message_id"]),
            "text": text,
            "message_time_iso": row["date_utc"],
            "edit_time_iso": None,
            "source": "archive",
        })

    return result


def _read_live_surge_rows():
    """
    현재 telegram_raw.db의 telegram_messages를 읽는다.
    """
    if not TELEGRAM_RAW_DB.exists():
        return []

    uri = f"file:{TELEGRAM_RAW_DB}?mode=ro"

    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT
                message_id,
                text,
                message_time_iso,
                edit_time_iso
            FROM telegram_messages
            WHERE chat_id = ?
              AND is_deleted = 0
              AND text IS NOT NULL
            ORDER BY message_time, message_id
        """, (
            MENGGUL_CHAT_ID,
        )).fetchall()

    result = []

    for row in rows:
        text = row["text"] or ""

        if not SURGE_SIGNAL_RE.search(text):
            continue

        result.append({
            "chat_id": MENGGUL_CHAT_ID,
            "message_id": int(row["message_id"]),
            "text": text,
            "message_time_iso": row["message_time_iso"],
            "edit_time_iso": row["edit_time_iso"],
            "source": "live",
        })

    return result


def get_all_surge_signals():
    """
    멍꿀단 전체 역사 기준 급등종목 신호.

    동일 Telegram message_id가 archive/live 양쪽에 있으면
    live 사본을 우선한다.

    반환되는 signal_stage는 유저가 급등매매를 켠 시점이 아니라
    해당 종목이 멍꿀단 급등신호에 역사상 등장한 순서다.
    """
    combined = {}

    # archive 먼저
    for row in _read_historical_surge_rows():
        key = (
            int(row["chat_id"]),
            int(row["message_id"]),
        )
        combined[key] = row

    # live를 나중에 넣어 같은 message_id이면 최신 사본으로 교체
    for row in _read_live_surge_rows():
        key = (
            int(row["chat_id"]),
            int(row["message_id"]),
        )
        combined[key] = row

    signals = []

    for row in combined.values():
        text = row["text"] or ""

        tags = SURGE_TAG_RE.findall(text)

        raw_symbol = (
            tags[-1].upper()
            if tags
            else None
        )

        symbol = normalize_surge_symbol(
            raw_symbol
        )

        # 종목 태그가 없는 메시지나
        # #OI급등종목 공지 메시지는 제외.
        if not symbol:
            continue

        signals.append({
            **row,
            "raw_symbol": raw_symbol,
            "symbol": symbol,
        })

    signals.sort(
        key=lambda row: (
            row["message_time_iso"] or "",
            int(row["message_id"]),
        )
    )

    stages = defaultdict(int)

    for row in signals:
        symbol = row["symbol"]

        stages[symbol] += 1

        row["signal_stage"] = stages[symbol]

    return signals


def get_surge_symbol_summary():
    """
    종목별 전체 신호 횟수와 최근 신호를 반환한다.
    최근 등장 종목이 위로 온다.
    """
    signals = get_all_surge_signals()

    summary = {}

    for row in signals:
        symbol = row["symbol"]

        current = summary.get(symbol)

        if current is None:
            summary[symbol] = {
                "symbol": symbol,
                "signal_count": 1,
                "first_signal_time": row[
                    "message_time_iso"
                ],
                "last_signal_time": row[
                    "message_time_iso"
                ],
                "last_message_id": row[
                    "message_id"
                ],
                "latest_stage": row[
                    "signal_stage"
                ],
            }
        else:
            current["signal_count"] += 1
            current["last_signal_time"] = row[
                "message_time_iso"
            ]
            current["last_message_id"] = row[
                "message_id"
            ]
            current["latest_stage"] = row[
                "signal_stage"
            ]

    rows = list(summary.values())

    rows.sort(
        key=lambda row: (
            row["last_signal_time"] or ""
        ),
        reverse=True,
    )

    return rows


def get_surge_symbol_entry_plan(user_id: int):
    """
    멍꿀단 전체 역사 기준으로 종목별:

      - 현재까지 신호 횟수
      - 다음 신호 차수
      - 다음 신호에 적용될 유저 비중

    을 계산한다.

    주의:
    - 주문하지 않는다.
    - surge_symbol_cycles를 차수 원본으로 사용하지 않는다.
    - Telegram 전체 신호 역사가 차수의 원본이다.
    """
    user_id = int(user_id)

    summary = get_surge_symbol_summary()

    result = []

    for row in summary:
        signal_count = int(
            row["signal_count"]
        )

        next_stage = signal_count + 1

        entry_percent = get_percent_for_stage(
            user_id,
            next_stage,
        )

        result.append({
            **row,
            "current_stage": signal_count,
            "next_stage": next_stage,
            "next_entry_percent": float(
                entry_percent
            ),
        })

    return result


def get_available_balance(user_id: int) -> float:
    """
    Bybit UNIFIED 계정의 현재 Available Balance를 읽는다.
    조회 전용.
    """
    from get_session import get_session

    session = get_session(int(user_id))

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    result = session.get_wallet_balance(
        accountType="UNIFIED",
    )

    rows = result.get("result", {}).get("list", [])

    if not rows:
        raise RuntimeError("Bybit wallet not found")

    value = rows[0].get("totalAvailableBalance")

    if value in (None, ""):
        raise RuntimeError(
            "totalAvailableBalance not found"
        )

    return float(value)


def get_symbol_max_leverage(
    user_id: int,
    symbol: str,
) -> float:
    """
    Bybit Linear USDT 종목의 거래소 허용 최대 레버리지.
    조회 전용.
    """
    from get_session import get_session

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    session = get_session(int(user_id))

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    result = session.get_instruments_info(
        category="linear",
        symbol=symbol,
    )

    rows = result.get("result", {}).get("list", [])

    if not rows:
        raise RuntimeError(
            f"Bybit instrument not found: {symbol}"
        )

    value = (
        rows[0]
        .get("leverageFilter", {})
        .get("maxLeverage")
    )

    if value in (None, ""):
        raise RuntimeError(
            f"maxLeverage not found: {symbol}"
        )

    return float(value)


def calculate_surge_entry_value(
    *,
    available: float,
    entry_percent: float,
    max_leverage: float,
):
    """
    entry_percent는 Available 대비 '요청 명목가치 비중'.

    예:
      Available 100
      entry_percent 1000
      max_leverage 5

      requested_value = 1000
      maximum_value   = 500
      actual_value    = 500
      required_cost   = 100
      actual_percent  = 500%
    """
    available = max(0.0, float(available))
    entry_percent = max(
        0.0,
        min(1000.0, float(entry_percent)),
    )
    max_leverage = max(
        0.0,
        float(max_leverage),
    )

    requested_value = (
        available * entry_percent / 100.0
    )

    maximum_value = (
        available * max_leverage
    )

    actual_value = min(
        requested_value,
        maximum_value,
    )

    if max_leverage > 0:
        required_cost = (
            actual_value / max_leverage
        )
    else:
        required_cost = 0.0

    if available > 0:
        actual_percent = (
            actual_value / available * 100.0
        )
    else:
        actual_percent = 0.0

    return {
        "available": available,
        "requested_percent": entry_percent,
        "requested_value": requested_value,
        "max_leverage": max_leverage,
        "maximum_value": maximum_value,
        "actual_percent": actual_percent,
        "actual_value": actual_value,
        "required_cost": required_cost,
        "limited": actual_value < requested_value,
    }


def get_symbol_risk_tiers(
    user_id: int,
    symbol: str,
):
    """
    Risk Limit 공개 진입점.

    - risk_limits.db에 있으면 로컬 DB 사용
    - 없으면 Bybit API 조회
    - 조회 성공 시 로컬 DB 저장
    - 이후 같은 종목은 API 호출 없이 사용
    """
    return get_symbol_risk_tiers_cached(
        user_id,
        symbol,
    )

def get_existing_position_value(
    user_id: int,
    symbol: str,
) -> float:
    """
    해당 종목의 현재 절대 Position Value 합계.

    현재 단계에서는 방향별 주문 계산 전이므로
    Long/Short가 모두 있다면 절대 명목가치를 합산한다.

    조회 전용.
    """
    from get_session import get_session

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    session = get_session(int(user_id))

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    result = session.get_positions(
        category="linear",
        symbol=symbol,
    )

    rows = result.get("result", {}).get("list", [])

    total = 0.0

    for row in rows:
        size = float(row.get("size") or 0)

        if size <= 0:
            continue

        value = row.get("positionValue")

        if value not in (None, ""):
            total += abs(float(value))
            continue

        # 혹시 positionValue가 비어 있으면
        # size × markPrice를 fallback으로 사용.
        mark_price = float(
            row.get("markPrice") or 0
        )

        total += abs(size * mark_price)

    return total


def calculate_risk_adjusted_entry(
    *,
    available: float,
    entry_percent: float,
    existing_position_value: float,
    risk_tiers,
):
    """
    주문하지 않는 Risk Limit 기반 계산기.

    1. Available × 비중으로 요청 명목가치 계산
    2. 기존 포지션 + 요청 명목가치 계산
    3. 그 총 Value를 수용하는 가장 높은 레버리지 티어 선택
    4. 해당 레버리지에서 Available 증거금으로 가능한지 검사
    5. 부족한 경우에만 신규 주문 명목가치를 축소

    실제 qtyStep/minNotional/maxMktOrderQty 보정은
    이후 주문 계산 단계에서 처리한다.
    """
    available = max(
        0.0,
        float(available),
    )

    entry_percent = max(
        0.0,
        min(1000.0, float(entry_percent)),
    )

    existing_position_value = max(
        0.0,
        float(existing_position_value),
    )

    requested_value = (
        available * entry_percent / 100.0
    )

    expected_position_value = (
        existing_position_value
        + requested_value
    )

    tiers = sorted(
        list(risk_tiers),
        key=lambda row: (
            -float(row["max_leverage"]),
            float(row["risk_limit_value"]),
        ),
    )

    if not tiers:
        raise RuntimeError(
            "No risk tiers supplied"
        )

    selected = None

    # 요청 명목가치를 그대로 유지하면서
    # 가능한 가장 높은 레버리지 티어 선택.
    for tier in tiers:
        if (
            expected_position_value
            <= float(tier["risk_limit_value"])
        ):
            selected = tier
            break

    risk_limited = False

    if selected is None:
        # 모든 Risk Limit을 초과한다.
        # 가장 큰 Risk Limit 티어를 선택하고
        # 신규 Value를 그 범위까지 축소한다.
        selected = max(
            tiers,
            key=lambda row: float(
                row["risk_limit_value"]
            ),
        )
        risk_limited = True

    selected_leverage = float(
        selected["max_leverage"]
    )

    selected_risk_limit = float(
        selected["risk_limit_value"]
    )

    # Risk Limit 안에서 신규 주문이 사용할 수 있는 최대 Value.
    risk_room = max(
        0.0,
        selected_risk_limit
        - existing_position_value,
    )

    value_after_risk_limit = min(
        requested_value,
        risk_room,
    )

    if value_after_risk_limit < requested_value:
        risk_limited = True

    # 현재 Available로 이 레버리지에서 만들 수 있는
    # 최대 신규 명목가치.
    funds_max_value = (
        available * selected_leverage
    )

    actual_value = min(
        value_after_risk_limit,
        funds_max_value,
    )

    funds_limited = (
        actual_value < value_after_risk_limit
    )

    if selected_leverage > 0:
        required_cost = (
            actual_value / selected_leverage
        )
    else:
        required_cost = 0.0

    if available > 0:
        actual_percent = (
            actual_value / available * 100.0
        )
    else:
        actual_percent = 0.0

    final_position_value = (
        existing_position_value
        + actual_value
    )

    reasons = []

    if risk_limited:
        reasons.append("RISK_LIMIT")

    if funds_limited:
        reasons.append("AVAILABLE")

    if not reasons:
        reasons.append("OK")

    return {
        "available": available,

        "requested_percent": entry_percent,
        "requested_value": requested_value,

        "existing_position_value":
            existing_position_value,

        "expected_position_value":
            expected_position_value,

        "selected_risk_id":
            selected["id"],

        "selected_risk_limit":
            selected_risk_limit,

        "selected_leverage":
            selected_leverage,

        "initial_margin":
            float(selected["initial_margin"]),

        "maintenance_margin":
            float(selected["maintenance_margin"]),

        "risk_room":
            risk_room,

        "funds_max_value":
            funds_max_value,

        "actual_percent":
            actual_percent,

        "actual_value":
            actual_value,

        "required_cost":
            required_cost,

        "final_position_value":
            final_position_value,

        "risk_limited":
            risk_limited,

        "funds_limited":
            funds_limited,

        "limited":
            actual_value < requested_value,

        "reason":
            "+".join(reasons),
    }


RISK_LIMIT_DB = BASE_DIR / "risk_limits.db"


def init_risk_limit_db():
    """
    Bybit Risk Limit 로컬 캐시 DB.
    종목별 전체 Risk Tier를 저장한다.
    """
    with sqlite3.connect(RISK_LIMIT_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS risk_limit_tiers (
                symbol TEXT NOT NULL,
                risk_id INTEGER NOT NULL,
                risk_limit_value REAL NOT NULL,
                max_leverage REAL NOT NULL,
                initial_margin REAL NOT NULL,
                maintenance_margin REAL NOT NULL,
                is_lowest_risk INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (symbol, risk_id)
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_risk_limit_tiers_symbol
            ON risk_limit_tiers(symbol)
        """)

        con.commit()


def get_cached_symbol_risk_tiers(
    symbol: str,
):
    """
    로컬 DB에서 Risk Tier 조회.

    없으면 빈 리스트 반환.
    API 호출하지 않는다.
    """
    init_risk_limit_db()

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    with sqlite3.connect(RISK_LIMIT_DB) as con:
        con.row_factory = sqlite3.Row

        rows = con.execute("""
            SELECT
                symbol,
                risk_id,
                risk_limit_value,
                max_leverage,
                initial_margin,
                maintenance_margin,
                is_lowest_risk,
                updated_at
            FROM risk_limit_tiers
            WHERE symbol = ?
            ORDER BY
                max_leverage DESC,
                risk_limit_value ASC
        """, (symbol,)).fetchall()

    return [
        {
            "id": int(row["risk_id"]),
            "symbol": row["symbol"],
            "risk_limit_value": float(
                row["risk_limit_value"]
            ),
            "max_leverage": float(
                row["max_leverage"]
            ),
            "initial_margin": float(
                row["initial_margin"]
            ),
            "maintenance_margin": float(
                row["maintenance_margin"]
            ),
            "is_lowest_risk": bool(
                row["is_lowest_risk"]
            ),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def save_symbol_risk_tiers(
    symbol: str,
    tiers,
):
    """
    한 종목의 Risk Tier 전체를 원자적으로 교체한다.
    """
    init_risk_limit_db()

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    tiers = list(tiers)

    if not tiers:
        raise RuntimeError(
            f"Cannot cache empty risk tiers: {symbol}"
        )

    with sqlite3.connect(RISK_LIMIT_DB) as con:
        # API에서 정상적인 전체 tier를 받은 뒤에만
        # 기존 캐시를 교체한다.
        con.execute(
            """
            DELETE FROM risk_limit_tiers
            WHERE symbol = ?
            """,
            (symbol,),
        )

        for tier in tiers:
            con.execute("""
                INSERT INTO risk_limit_tiers (
                    symbol,
                    risk_id,
                    risk_limit_value,
                    max_leverage,
                    initial_margin,
                    maintenance_margin,
                    is_lowest_risk,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                symbol,
                int(tier["id"]),
                float(tier["risk_limit_value"]),
                float(tier["max_leverage"]),
                float(tier["initial_margin"]),
                float(tier["maintenance_margin"]),
                1 if tier.get("is_lowest_risk") else 0,
            ))

        con.commit()


def fetch_symbol_risk_tiers_from_bybit(
    user_id: int,
    symbol: str,
):
    """
    Bybit API에서 직접 Risk Tier 전체 조회.

    캐시 조회는 하지 않는다.
    """
    from get_session import get_session

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    session = get_session(int(user_id))

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    result = session.get_risk_limit(
        category="linear",
        symbol=symbol,
    )

    rows = result.get("result", {}).get("list", [])

    if not rows:
        raise RuntimeError(
            f"Risk limit not found: {symbol}"
        )

    tiers = []

    for row in rows:
        tiers.append({
            "id": int(row["id"]),
            "symbol": symbol,
            "risk_limit_value": float(
                row["riskLimitValue"]
            ),
            "max_leverage": float(
                row["maxLeverage"]
            ),
            "initial_margin": float(
                row["initialMargin"]
            ),
            "maintenance_margin": float(
                row["maintenanceMargin"]
            ),
            "is_lowest_risk": bool(
                row.get("isLowestRisk")
            ),
        })

    tiers.sort(
        key=lambda row: (
            -row["max_leverage"],
            row["risk_limit_value"],
        )
    )

    return tiers


def get_symbol_risk_tiers_cached(
    user_id: int,
    symbol: str,
):
    """
    Risk Limit cache-first 조회.

    1. risk_limits.db 확인
    2. 존재하면 API 호출 없이 반환
    3. 없으면 Bybit API 조회
    4. 성공한 전체 Tier를 DB 저장
    5. 반환

    DB에도 없고 API도 실패하면 예외를 그대로 발생시켜
    해당 종목의 진입을 안전하게 차단할 수 있게 한다.
    """
    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    cached = get_cached_symbol_risk_tiers(
        symbol
    )

    if cached:
        return cached

    tiers = fetch_symbol_risk_tiers_from_bybit(
        user_id,
        symbol,
    )

    save_symbol_risk_tiers(
        symbol,
        tiers,
    )

    return get_cached_symbol_risk_tiers(
        symbol
    )


init_risk_limit_db()


def build_surge_entry_plan(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
):
    """
    급등매매 단일 종목 진입 계획.

    아직 주문하지 않는다.
    레버리지도 변경하지 않는다.

    계산 순서:
      1. Telegram 전체 역사에서 현재 신호 차수 확인
      2. 해당 차수에 설정된 비중 확인
      3. Available Balance 확인
      4. 기존 포지션 명목가치 확인
      5. Risk Limit Tier 확인 (cache-first)
      6. 적절한 레버리지 / 실제 가능한 명목가치 계산

    entry_percent를 직접 주면 테스트용으로 해당 비중을 사용한다.
    """

    user_id = int(user_id)

    base_symbol = normalize_surge_symbol(symbol)

    if not base_symbol:
        raise ValueError(
            f"Invalid surge symbol: {symbol!r}"
        )

    trading_symbol = (
        base_symbol
        if base_symbol.endswith("USDT")
        else base_symbol + "USDT"
    )

    # --------------------------------------------------------
    # Telegram 전체 역사 기준 현재까지 신호 횟수
    # --------------------------------------------------------

    summary = get_surge_symbol_summary()

    signal_row = next(
        (
            row
            for row in summary
            if row["symbol"] == base_symbol
        ),
        None,
    )

    if signal_row is None:
        signal_count = 0
        last_signal_time = None
        last_message_id = None
    else:
        signal_count = int(
            signal_row["signal_count"]
        )
        last_signal_time = signal_row.get(
            "last_signal_time"
        )
        last_message_id = signal_row.get(
            "last_message_id"
        )

    # 현재 DB에 이미 들어온 최신 신호를
    # 실제 실행 대상으로 보는 계획이므로
    # 적용 차수는 현재 누적 신호 횟수.
    #
    # 신호가 아직 한 번도 없는 종목을 수동 테스트하면
    # 1차로 계산한다.
    signal_stage = max(1, signal_count)

    if entry_percent is None:
        requested_percent = get_percent_for_stage(
            user_id,
            signal_stage,
        )
    else:
        requested_percent = float(
            entry_percent
        )

    # --------------------------------------------------------
    # 계정 / 포지션 / Risk Limit
    # --------------------------------------------------------

    available = get_available_balance(
        user_id
    )

    existing_position_value = (
        get_existing_position_value(
            user_id,
            trading_symbol,
        )
    )

    risk_tiers = get_symbol_risk_tiers(
        user_id,
        trading_symbol,
    )

    calculation = calculate_risk_adjusted_entry(
        available=available,
        entry_percent=requested_percent,
        existing_position_value=(
            existing_position_value
        ),
        risk_tiers=risk_tiers,
    )

    return {
        "user_id": user_id,
        "symbol": base_symbol,
        "trading_symbol": trading_symbol,

        "signal_count": signal_count,
        "signal_stage": signal_stage,

        "last_signal_time":
            last_signal_time,

        "last_message_id":
            last_message_id,

        "entry_percent":
            float(requested_percent),

        **calculation,
    }


def get_symbol_order_info(
    user_id: int,
    symbol: str,
):
    """
    주문 수량 계산에 필요한 종목 정보를 반환한다.

    정적 instrument 규칙:
      instruments.db 사용

    실시간 가격:
      Bybit ticker API 사용

    instruments.db에 종목이 없으면 신규상장 가능성이 있으므로
    instrument cache 전체 refresh를 1회 수행한 뒤 다시 조회한다.
    """
    from get_session import get_session

    user_id = int(user_id)

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # --------------------------------------------------------
    # 1. Static instrument info -> local DB
    # --------------------------------------------------------

    instrument = get_cached_instrument(
        symbol
    )

    instrument_refreshed = False

    if instrument is None:
        refresh_instrument_cache(
            user_id
        )

        instrument_refreshed = True

        instrument = get_cached_instrument(
            symbol
        )

    if instrument is None:
        raise RuntimeError(
            f"Instrument not found after refresh: {symbol}"
        )

    if str(
        instrument.get("status") or ""
    ) != "Trading":
        raise RuntimeError(
            "Instrument is not Trading: "
            f"{symbol} "
            f"status={instrument.get('status')}"
        )

    # --------------------------------------------------------
    # 2. Realtime price -> ticker API
    # --------------------------------------------------------

    session = get_session(user_id)

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    ticker_response = session.get_tickers(
        category="linear",
        symbol=symbol,
    )

    ticker_result = ticker_response.get(
        "result",
        {}
    )

    ticker_rows = ticker_result.get(
        "list",
        []
    )

    if not ticker_rows:
        raise RuntimeError(
            f"Ticker not found: {symbol}"
        )

    ticker = ticker_rows[0]

    # markPrice 우선.
    # 없으면 lastPrice fallback.
    price = float(
        ticker.get("markPrice")
        or ticker.get("lastPrice")
        or 0
    )

    if price <= 0:
        raise RuntimeError(
            f"Invalid ticker price: {symbol}"
        )

    return {
        "symbol":
            symbol,

        "price":
            price,

        "min_order_qty":
            float(
                instrument["min_order_qty"]
            ),

        "qty_step":
            float(
                instrument["qty_step"]
            ),

        "max_market_qty":
            float(
                instrument["max_market_qty"]
            ),

        "max_order_qty":
            float(
                instrument["max_order_qty"]
            ),

        "min_notional":
            float(
                instrument["min_notional"]
            ),

        "tick_size":
            float(
                instrument["tick_size"]
            ),

        "max_leverage":
            float(
                instrument["max_leverage"]
            ),

        "instrument_source":
            "DB",

        "instrument_refreshed":
            instrument_refreshed,

        "price_source":
            "TICKER",
    }

def calculate_surge_order_qty(
    *,
    actual_value: float,
    price: float,
    qty_step: float,
    min_order_qty: float,
    max_market_qty: float,
    min_notional: float,
):
    """
    최종 주문 명목가치를 실제 Bybit 주문수량으로 변환한다.

    핵심:
      - 목표 명목가치를 초과하지 않도록 qtyStep 단위로 내림
      - 시장가 최대수량 제한 적용
      - 최소수량 / 최소명목가치 검증

    주문하지 않는다.
    """
    from decimal import (
        Decimal,
        ROUND_DOWN,
    )

    value_d = Decimal(str(actual_value))
    price_d = Decimal(str(price))
    step_d = Decimal(str(qty_step))
    min_qty_d = Decimal(str(min_order_qty))
    max_qty_d = Decimal(str(max_market_qty))
    min_notional_d = Decimal(str(min_notional))

    if value_d <= 0:
        raise ValueError(
            "actual_value must be positive"
        )

    if price_d <= 0:
        raise ValueError(
            "price must be positive"
        )

    if step_d <= 0:
        raise ValueError(
            "qty_step must be positive"
        )

    raw_qty = value_d / price_d

    steps = (
        raw_qty / step_d
    ).to_integral_value(
        rounding=ROUND_DOWN
    )

    qty = steps * step_d

    market_qty_limited = False

    if max_qty_d > 0 and qty > max_qty_d:
        qty = max_qty_d

        # maxMktOrderQty 자체가 step에 정확히
        # 맞지 않는 경우도 안전하게 다시 내림.
        steps = (
            qty / step_d
        ).to_integral_value(
            rounding=ROUND_DOWN
        )

        qty = steps * step_d
        market_qty_limited = True

    final_notional = qty * price_d

    valid = True
    reason = "OK"

    if qty <= 0:
        valid = False
        reason = "ZERO_QTY"

    elif min_qty_d > 0 and qty < min_qty_d:
        valid = False
        reason = "BELOW_MIN_QTY"

    elif (
        min_notional_d > 0
        and final_notional < min_notional_d
    ):
        valid = False
        reason = "BELOW_MIN_NOTIONAL"

    return {
        "raw_qty": float(raw_qty),
        "qty": float(qty),
        "price": float(price_d),

        "target_notional":
            float(value_d),

        "final_notional":
            float(final_notional),

        "min_order_qty":
            float(min_qty_d),

        "qty_step":
            float(step_d),

        "max_market_qty":
            float(max_qty_d),

        "min_notional":
            float(min_notional_d),

        "market_qty_limited":
            market_qty_limited,

        "valid":
            valid,

        "reason":
            reason,
    }


def build_surge_order_plan(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
):
    """
    진입계획 + 실제 주문수량까지 계산.

    주문하지 않는다.
    레버리지도 변경하지 않는다.
    """
    entry = build_surge_entry_plan(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    order_info = get_symbol_order_info(
        user_id,
        entry["trading_symbol"],
    )

    qty_plan = calculate_surge_order_qty(
        actual_value=entry["actual_value"],
        price=order_info["price"],
        qty_step=order_info["qty_step"],
        min_order_qty=(
            order_info["min_order_qty"]
        ),
        max_market_qty=(
            order_info["max_market_qty"]
        ),
        min_notional=(
            order_info["min_notional"]
        ),
    )

    entry_limit_reason = entry.get(
        "reason"
    )

    order_reason = qty_plan.get(
        "reason"
    )

    result = {
        **entry,
        **qty_plan,
    }

    result["entry_limit_reason"] = (
        entry_limit_reason
    )

    result["order_reason"] = (
        order_reason
    )

    # 모호한 공용 reason 키는 제거한다.
    result.pop("reason", None)

    return result


def set_surge_leverage(
    user_id: int,
    symbol: str,
    leverage: float,
    *,
    dry_run: bool = True,
):
    """
    급등매매용 레버리지 설정.

    dry_run=True:
      - API 변경 없음
      - 설정 예정값만 반환

    dry_run=False:
      - Bybit set_leverage 호출
      - buy/sell leverage를 동일하게 설정

    주문은 하지 않는다.
    """
    from decimal import Decimal
    from get_session import get_session

    user_id = int(user_id)

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    leverage_d = Decimal(str(leverage))

    if leverage_d <= 0:
        raise ValueError(
            f"Invalid leverage: {leverage}"
        )

    # 25.0 -> "25"
    # 4.5  -> "4.5"
    leverage_text = format(
        leverage_d.normalize(),
        "f",
    )

    result = {
        "user_id": user_id,
        "symbol": symbol,
        "leverage": float(leverage_d),
        "leverage_text": leverage_text,
        "dry_run": bool(dry_run),
        "changed": False,
        "response": None,
    }

    if dry_run:
        return result

    session = get_session(user_id)

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    response = session.set_leverage(
        category="linear",
        symbol=symbol,
        buyLeverage=leverage_text,
        sellLeverage=leverage_text,
    )

    ret_code = int(
        response.get("retCode", -1)
    )

    # Bybit에서 정상 성공만 허용.
    if ret_code != 0:
        raise RuntimeError(
            f"set_leverage failed: "
            f"symbol={symbol} "
            f"leverage={leverage_text} "
            f"response={response!r}"
        )

    result["changed"] = True
    result["response"] = response

    return result


def prepare_surge_order(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
    apply_leverage: bool = False,
):
    """
    급등매매 주문 직전 준비 단계.

    1. Risk/Available/비중 계산
    2. 실제 주문 qty 계산
    3. 선택된 레버리지 준비
    4. apply_leverage=True일 때만 실제 레버리지 변경

    아직 주문 자체는 하지 않는다.
    """
    plan = build_surge_order_plan(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not plan["valid"]:
        raise RuntimeError(
            "Invalid surge order plan: "
            f"{plan['order_reason']}"
        )

    leverage_result = ensure_surge_leverage(
        user_id,
        plan["trading_symbol"],
        plan["selected_leverage"],
        dry_run=not apply_leverage,
    )

    return {
        **plan,
        "leverage_apply":
            leverage_result,
    }


def get_current_symbol_leverage(
    user_id: int,
    symbol: str,
):
    """
    현재 Bybit 포지션 설정에서 해당 종목의 레버리지를 읽는다.

    Hedge Mode에서는 Buy/Sell positionIdx가 따로 존재할 수 있으므로
    발견된 레버리지를 모두 반환한다.

    조회 전용.
    """
    from get_session import get_session

    user_id = int(user_id)

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    session = get_session(user_id)

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    result = session.get_positions(
        category="linear",
        symbol=symbol,
    )

    rows = (
        result
        .get("result", {})
        .get("list", [])
    )

    leverages = []

    for row in rows:
        value = row.get("leverage")

        if value in (None, ""):
            continue

        try:
            leverage = float(value)
        except (TypeError, ValueError):
            continue

        if leverage > 0:
            leverages.append({
                "position_idx": int(
                    row.get("positionIdx") or 0
                ),
                "side": row.get("side") or "",
                "size": float(
                    row.get("size") or 0
                ),
                "leverage": leverage,
            })

    return {
        "symbol": symbol,
        "positions": leverages,
        "leverages": sorted({
            row["leverage"]
            for row in leverages
        }),
    }


def ensure_surge_leverage(
    user_id: int,
    symbol: str,
    leverage: float,
    *,
    dry_run: bool = True,
):
    """
    목표 레버리지와 현재 레버리지를 비교한다.

    현재 설정이 모두 목표값이면:
      -> set_leverage 호출 생략

    다르면:
      dry_run=True
        -> 변경 예정만 반환

      dry_run=False
        -> 실제 set_leverage 실행
    """
    target = float(leverage)

    current = get_current_symbol_leverage(
        user_id,
        symbol,
    )

    current_values = current["leverages"]

    already_set = (
        bool(current_values)
        and all(
            abs(value - target) < 1e-12
            for value in current_values
        )
    )

    if already_set:
        return {
            "user_id": int(user_id),
            "symbol": current["symbol"],
            "target_leverage": target,
            "current_leverages": current_values,
            "already_set": True,
            "skipped": True,
            "dry_run": bool(dry_run),
            "changed": False,
            "response": None,
        }

    if dry_run:
        return {
            "user_id": int(user_id),
            "symbol": current["symbol"],
            "target_leverage": target,
            "current_leverages": current_values,
            "already_set": False,
            "skipped": False,
            "dry_run": True,
            "changed": False,
            "response": None,
        }

    result = set_surge_leverage(
        user_id,
        current["symbol"],
        target,
        dry_run=False,
    )

    return {
        "user_id": int(user_id),
        "symbol": current["symbol"],
        "target_leverage": target,
        "current_leverages": current_values,
        "already_set": False,
        "skipped": False,
        "dry_run": False,
        "changed": result["changed"],
        "response": result["response"],
    }


def rebuild_surge_order_after_leverage(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
):
    """
    레버리지 설정 이후 호출할 최종 주문계획 재계산.

    핵심:
      - Available을 새로 읽는다.
      - 기존 포지션도 새로 읽는다.
      - Risk Tier를 다시 적용한다.
      - 현재가도 새로 읽는다.
      - 최종 qty를 다시 계산한다.

    주문하지 않는다.
    레버리지도 변경하지 않는다.
    """
    plan = build_surge_order_plan(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not plan["valid"]:
        raise RuntimeError(
            "Invalid final surge order plan: "
            f"{plan['order_reason']}"
        )

    return {
        **plan,
        "final_recalculated": True,
    }


def prepare_surge_order_final(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
    apply_leverage: bool = False,
):
    """
    실제 주문 직전의 전체 준비 흐름.

    순서:
      1. 1차 계획 계산
      2. 목표 Risk Tier / 레버리지 결정
      3. 레버리지 확인 또는 설정
      4. Available / Position / Price 재조회
      5. 최종 명목가치 / qty 재계산

    아직 주문 자체는 하지 않는다.

    apply_leverage=False:
      전체 흐름을 dry-run으로 검증한다.

    apply_leverage=True:
      필요한 경우 실제 레버리지만 변경한 뒤
      최종 주문계획을 다시 계산한다.
      주문은 하지 않는다.
    """
    initial_plan = build_surge_order_plan(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not initial_plan["valid"]:
        raise RuntimeError(
            "Invalid initial surge order plan: "
            f"{initial_plan['order_reason']}"
        )

    leverage_result = ensure_surge_leverage(
        user_id,
        initial_plan["trading_symbol"],
        initial_plan["selected_leverage"],
        dry_run=not apply_leverage,
    )

    final_plan = rebuild_surge_order_after_leverage(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    return {
        "initial_plan": initial_plan,
        "leverage_apply": leverage_result,
        "final_plan": final_plan,

        "initial_available":
            initial_plan["available"],

        "final_available":
            final_plan["available"],

        "initial_actual_value":
            initial_plan["actual_value"],

        "final_actual_value":
            final_plan["actual_value"],

        "initial_qty":
            initial_plan["qty"],

        "final_qty":
            final_plan["qty"],

        "final_order_valid":
            final_plan["valid"],

        "final_entry_limit_reason":
            final_plan["entry_limit_reason"],

        "final_order_reason":
            final_plan["order_reason"],
    }


def validate_surge_leverage_stability(
    initial_plan,
    final_plan,
):
    """
    1차 계획과 최종 재계산의 Risk Tier / 레버리지 안정성 검증.

    주문하지 않는다.
    레버리지를 변경하지 않는다.

    최종 재계산에서 필요한 레버리지가 달라졌다면
    실제 주문 단계로 진행하지 않도록 한다.
    """
    initial_leverage = float(
        initial_plan["selected_leverage"]
    )

    final_leverage = float(
        final_plan["selected_leverage"]
    )

    initial_risk_id = initial_plan.get(
        "selected_risk_id"
    )

    final_risk_id = final_plan.get(
        "selected_risk_id"
    )

    leverage_stable = (
        abs(
            initial_leverage
            - final_leverage
        ) < 1e-12
    )

    risk_tier_stable = (
        initial_risk_id
        == final_risk_id
    )

    stable = (
        leverage_stable
        and risk_tier_stable
    )

    if stable:
        reason = "OK"

    elif not leverage_stable:
        reason = "LEVERAGE_CHANGED_AFTER_RECALC"

    else:
        reason = "RISK_TIER_CHANGED_AFTER_RECALC"

    return {
        "stable": stable,

        "leverage_stable":
            leverage_stable,

        "risk_tier_stable":
            risk_tier_stable,

        "initial_leverage":
            initial_leverage,

        "final_leverage":
            final_leverage,

        "initial_risk_id":
            initial_risk_id,

        "final_risk_id":
            final_risk_id,

        "initial_risk_limit":
            initial_plan.get(
                "selected_risk_limit"
            ),

        "final_risk_limit":
            final_plan.get(
                "selected_risk_limit"
            ),

        "reason":
            reason,
    }


def build_surge_execution_preview(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
):
    """
    실제 주문 직전 전체 READ-ONLY preview.

    - 주문 없음
    - 레버리지 변경 없음
    - 최종 재계산
    - Risk Tier / leverage 안정성 검증
    """
    prepared = prepare_surge_order_final(
        user_id,
        symbol,
        entry_percent=entry_percent,
        apply_leverage=False,
    )

    initial = prepared["initial_plan"]
    final = prepared["final_plan"]

    stability = (
        validate_surge_leverage_stability(
            initial,
            final,
        )
    )

    executable = (
        bool(final["valid"])
        and bool(stability["stable"])
        and float(final["qty"]) > 0
    )

    return {
        **prepared,

        "stability":
            stability,

        "executable":
            executable,
    }


def rebuild_surge_order_for_available(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
):
    """
    주문 직전 또는 자금 부족 발생 시 사용하는 재계산 함수.

    현재 시점의:
      - Available
      - 기존 Position Value
      - Risk Tier
      - 현재가
      - qtyStep

    를 전부 다시 읽어서 실제 가능한 qty를 새로 만든다.

    주문하지 않는다.
    레버리지도 변경하지 않는다.
    """
    plan = build_surge_order_plan(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not plan["valid"]:
        return {
            **plan,
            "retryable": False,
            "recalc_reason":
                plan["order_reason"],
        }

    if float(plan["qty"]) <= 0:
        return {
            **plan,
            "retryable": False,
            "recalc_reason":
                "ZERO_QTY",
        }

    return {
        **plan,
        "retryable": True,
        "recalc_reason": "OK",
    }


def calculate_reduced_surge_order(
    user_id: int,
    symbol: str,
    *,
    entry_percent: float | None = None,
    reduction_factor: float = 0.98,
):
    """
    자금 부족 주문 재시도용 축소 계획.

    먼저 최신 Available 기준으로 전체 계획을 다시 만든 뒤,
    그 결과 명목가치에서 reduction_factor 만큼만 사용한다.

    기본 0.98:
      최신 계산 가능 명목가치의 98%만 주문 대상으로 사용.

    이렇게 약간의 여유를 두어 시장가 체결가 변동,
    수수료/증거금 변화 때문에 같은 주문이 다시
    자금 부족으로 거절될 가능성을 낮춘다.

    주문하지 않는다.
    """
    reduction_factor = float(
        reduction_factor
    )

    if not (
        0.0 < reduction_factor < 1.0
    ):
        raise ValueError(
            "reduction_factor must be "
            "greater than 0 and less than 1"
        )

    fresh = rebuild_surge_order_for_available(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not fresh["retryable"]:
        return {
            **fresh,
            "reduced": False,
            "reduction_factor":
                reduction_factor,
        }

    reduced_value = (
        float(fresh["actual_value"])
        * reduction_factor
    )

    order_info = get_symbol_order_info(
        user_id,
        fresh["trading_symbol"],
    )

    qty_plan = calculate_surge_order_qty(
        actual_value=reduced_value,
        price=order_info["price"],
        qty_step=order_info["qty_step"],
        min_order_qty=(
            order_info["min_order_qty"]
        ),
        max_market_qty=(
            order_info["max_market_qty"]
        ),
        min_notional=(
            order_info["min_notional"]
        ),
    )

    result = {
        **fresh,
        **qty_plan,
    }

    # 위 merge에서 reason 충돌 방지
    result.pop("reason", None)

    result["entry_limit_reason"] = (
        fresh["entry_limit_reason"]
    )

    result["order_reason"] = (
        qty_plan["reason"]
    )

    result["original_actual_value"] = (
        float(fresh["actual_value"])
    )

    result["reduced_actual_value"] = (
        reduced_value
    )

    result["reduction_factor"] = (
        reduction_factor
    )

    result["reduced"] = True

    result["retryable"] = (
        bool(qty_plan["valid"])
        and float(qty_plan["qty"]) > 0
    )

    result["recalc_reason"] = (
        "OK"
        if result["retryable"]
        else qty_plan["reason"]
    )

    return result


# ============================================================
# Surge execution idempotency
# ============================================================

SURGE_EXECUTION_DB = BASE_DIR / "surge_executions.db"


def init_surge_execution_db():
    """
    멍꿀단 급등매매 전용 실행 중복방지 DB.

    Telegram message 하나에 대해
    user별 실제 진입은 한 번만 허용한다.
    """
    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS surge_executions (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,

                symbol TEXT NOT NULL,
                side TEXT NOT NULL,

                signal_stage INTEGER,
                entry_percent REAL,

                order_link_id TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'CLAIMED',

                order_id TEXT,
                qty REAL,
                notional REAL,
                leverage REAL,
                risk_id INTEGER,
                risk_limit REAL,

                error TEXT,

                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (
                    user_id,
                    chat_id,
                    message_id
                ),

                UNIQUE (order_link_id)
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_surge_executions_status
            ON surge_executions(status)
        """)

        con.commit()


def surge_order_link_id(
    user_id: int,
    chat_id: int,
    message_id: int,
):
    """
    멍꿀단 급등매매 전용 Bybit orderLinkId.

    기존 Telegram 자동매매의 tgs-* 와 구분하기 위해
    srg-* prefix를 사용한다.
    """
    import hashlib

    user_id = int(user_id)
    chat_id = int(chat_id or 0)
    message_id = int(message_id)

    raw = (
        f"surge:{user_id}:{chat_id}:{message_id}"
    ).encode()

    digest = hashlib.sha256(
        raw
    ).hexdigest()[:16]

    return f"srg-{user_id}-{digest}"


def claim_surge_execution(
    user_id: int,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    *,
    signal_stage=None,
    entry_percent=None,
):
    """
    실제 주문 직전에 execution을 CLAIM한다.

    같은
      user_id + chat_id + message_id

    가 이미 존재하면 False를 반환하여
    중복 주문을 차단한다.
    """
    init_surge_execution_db()

    user_id = int(user_id)
    chat_id = int(chat_id or 0)
    message_id = int(message_id)

    symbol = str(symbol).upper().strip()
    side = str(side).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    if side not in ("LONG", "SHORT"):
        raise ValueError(
            f"Invalid surge side: {side}"
        )

    order_link_id = surge_order_link_id(
        user_id,
        chat_id,
        message_id,
    )

    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        cur = con.execute("""
            INSERT OR IGNORE INTO surge_executions (
                user_id,
                chat_id,
                message_id,
                symbol,
                side,
                signal_stage,
                entry_percent,
                order_link_id,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CLAIMED')
        """, (
            user_id,
            chat_id,
            message_id,
            symbol,
            side,
            (
                int(signal_stage)
                if signal_stage is not None
                else None
            ),
            (
                float(entry_percent)
                if entry_percent is not None
                else None
            ),
            order_link_id,
        ))

        con.commit()

        claimed = cur.rowcount == 1

    return {
        "claimed": claimed,
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "symbol": symbol,
        "side": side,
        "order_link_id": order_link_id,
    }


def get_surge_execution(
    user_id: int,
    chat_id: int,
    message_id: int,
):
    """
    execution 현재 상태 조회.
    """
    init_surge_execution_db()

    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        con.row_factory = sqlite3.Row

        row = con.execute("""
            SELECT *
            FROM surge_executions
            WHERE user_id=?
              AND chat_id=?
              AND message_id=?
        """, (
            int(user_id),
            int(chat_id or 0),
            int(message_id),
        )).fetchone()

    return dict(row) if row else None


def complete_surge_execution(
    user_id: int,
    chat_id: int,
    message_id: int,
    *,
    order_id=None,
    qty=None,
    notional=None,
    leverage=None,
    risk_id=None,
    risk_limit=None,
):
    """
    주문이 Bybit에 정상 접수된 execution을 ACCEPTED 처리한다.

    체결 완료(FILLED)와는 구분한다.
    """
    init_surge_execution_db()

    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        cur = con.execute("""
            UPDATE surge_executions
            SET
                status='ACCEPTED',
                order_id=?,
                qty=?,
                notional=?,
                leverage=?,
                risk_id=?,
                risk_limit=?,
                error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
              AND chat_id=?
              AND message_id=?
              AND status='CLAIMED'
        """, (
            (
                str(order_id)
                if order_id is not None
                else None
            ),
            (
                float(qty)
                if qty is not None
                else None
            ),
            (
                float(notional)
                if notional is not None
                else None
            ),
            (
                float(leverage)
                if leverage is not None
                else None
            ),
            (
                int(risk_id)
                if risk_id is not None
                else None
            ),
            (
                float(risk_limit)
                if risk_limit is not None
                else None
            ),
            int(user_id),
            int(chat_id or 0),
            int(message_id),
        ))

        con.commit()

        return cur.rowcount == 1


def fail_surge_execution(
    user_id: int,
    chat_id: int,
    message_id: int,
    error,
):
    """
    주문을 보내지 못했다는 것이 확실한 경우 FAILED 처리.

    행을 삭제하지 않는다.
    실패 이력을 남겨서 무조건적인 자동 재주문을 막는다.
    """
    init_surge_execution_db()

    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        cur = con.execute("""
            UPDATE surge_executions
            SET
                status='FAILED',
                error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
              AND chat_id=?
              AND message_id=?
              AND status='CLAIMED'
        """, (
            str(error),
            int(user_id),
            int(chat_id or 0),
            int(message_id),
        ))

        con.commit()

        return cur.rowcount == 1


init_surge_execution_db()


# ============================================================
# Surge order reconcile
# ============================================================

SURGE_TERMINAL_ORDER_STATUSES = {
    "Cancelled",
    "Rejected",
    "Deactivated",
}

SURGE_FILLED_ORDER_STATUSES = {
    "Filled",
}


def reconcile_surge_order(
    user_id: int,
    order_link_id: str,
    symbol: str | None = None,
):
    """
    srg-* 주문을 Bybit에서 조회해 실제 상태를 판정한다.

    조회 순서:
      1. open/realtime orders
      2. order history
      3. executions

    주문은 생성하지 않는다.

    반환 state:
      ACCEPTED
      FILLED
      TERMINAL_FAILED
      NOT_FOUND
      UNKNOWN
    """
    from get_session import get_session

    user_id = int(user_id)
    order_link_id = str(order_link_id).strip()

    if not order_link_id:
        raise ValueError("order_link_id is required")

    trading_symbol = None

    if symbol:
        trading_symbol = str(symbol).upper().strip()

        if not trading_symbol.endswith("USDT"):
            trading_symbol += "USDT"

    session = get_session(user_id)

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    errors = []

    # --------------------------------------------------------
    # 1. Realtime / open-order lookup
    # --------------------------------------------------------

    try:
        params = {
            "category": "linear",
            "orderLinkId": order_link_id,
        }

        if trading_symbol:
            params["symbol"] = trading_symbol

        result = session.get_open_orders(**params)

        rows = (
            result
            .get("result", {})
            .get("list", [])
        )

        if rows:
            row = rows[0]

            status = str(
                row.get("orderStatus") or ""
            )

            if status in SURGE_FILLED_ORDER_STATUSES:
                state = "FILLED"

            elif status in SURGE_TERMINAL_ORDER_STATUSES:
                state = "TERMINAL_FAILED"

            else:
                state = "ACCEPTED"

            return {
                "state": state,
                "source": "open_orders",
                "order_id": row.get("orderId"),
                "order_link_id": order_link_id,
                "order_status": status,
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "qty": row.get("qty"),
                "cum_exec_qty": row.get("cumExecQty"),
                "avg_price": row.get("avgPrice"),
                "raw": row,
                "errors": errors,
            }

    except Exception as exc:
        errors.append(
            f"open_orders: {type(exc).__name__}: {exc}"
        )

    # --------------------------------------------------------
    # 2. Order history lookup
    # --------------------------------------------------------

    try:
        params = {
            "category": "linear",
            "orderLinkId": order_link_id,
            "limit": 50,
        }

        if trading_symbol:
            params["symbol"] = trading_symbol

        result = session.get_order_history(**params)

        rows = (
            result
            .get("result", {})
            .get("list", [])
        )

        if rows:
            row = rows[0]

            status = str(
                row.get("orderStatus") or ""
            )

            if status in SURGE_FILLED_ORDER_STATUSES:
                state = "FILLED"

            elif status in SURGE_TERMINAL_ORDER_STATUSES:
                state = "TERMINAL_FAILED"

            else:
                state = "ACCEPTED"

            return {
                "state": state,
                "source": "order_history",
                "order_id": row.get("orderId"),
                "order_link_id": order_link_id,
                "order_status": status,
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "qty": row.get("qty"),
                "cum_exec_qty": row.get("cumExecQty"),
                "avg_price": row.get("avgPrice"),
                "raw": row,
                "errors": errors,
            }

    except Exception as exc:
        errors.append(
            f"order_history: {type(exc).__name__}: {exc}"
        )

    # --------------------------------------------------------
    # 3. Execution lookup
    # --------------------------------------------------------

    try:
        params = {
            "category": "linear",
            "orderLinkId": order_link_id,
            "limit": 100,
        }

        if trading_symbol:
            params["symbol"] = trading_symbol

        result = session.get_executions(**params)

        rows = (
            result
            .get("result", {})
            .get("list", [])
        )

        if rows:
            total_qty = 0.0
            total_value = 0.0

            order_id = None
            found_symbol = None
            found_side = None

            for row in rows:
                try:
                    qty = float(
                        row.get("execQty") or 0
                    )
                except Exception:
                    qty = 0.0

                try:
                    price = float(
                        row.get("execPrice") or 0
                    )
                except Exception:
                    price = 0.0

                total_qty += qty
                total_value += qty * price

                if order_id is None:
                    order_id = row.get("orderId")

                if found_symbol is None:
                    found_symbol = row.get("symbol")

                if found_side is None:
                    found_side = row.get("side")

            avg_price = (
                total_value / total_qty
                if total_qty > 0
                else 0.0
            )

            return {
                "state": "FILLED",
                "source": "executions",
                "order_id": order_id,
                "order_link_id": order_link_id,
                "order_status": "Filled",
                "symbol": found_symbol,
                "side": found_side,
                "qty": total_qty,
                "cum_exec_qty": total_qty,
                "avg_price": avg_price,
                "raw": rows,
                "errors": errors,
            }

    except Exception as exc:
        errors.append(
            f"executions: {type(exc).__name__}: {exc}"
        )

    # --------------------------------------------------------
    # 조회 자체가 실패했다면 NOT_FOUND라고 단정하면 안 된다.
    # --------------------------------------------------------

    if errors:
        return {
            "state": "UNKNOWN",
            "source": None,
            "order_id": None,
            "order_link_id": order_link_id,
            "order_status": None,
            "symbol": trading_symbol,
            "side": None,
            "qty": None,
            "cum_exec_qty": None,
            "avg_price": None,
            "raw": None,
            "errors": errors,
        }

    # 세 조회가 모두 정상적으로 끝났지만 어디에도 없다.
    return {
        "state": "NOT_FOUND",
        "source": None,
        "order_id": None,
        "order_link_id": order_link_id,
        "order_status": None,
        "symbol": trading_symbol,
        "side": None,
        "qty": None,
        "cum_exec_qty": None,
        "avg_price": None,
        "raw": None,
        "errors": [],
    }


def update_surge_execution_from_reconcile(
    user_id: int,
    chat_id: int,
    message_id: int,
    reconcile_result,
):
    """
    Bybit reconcile 결과를 surge_executions에 반영한다.

    중요:
    - ACCEPTED / FILLED / TERMINAL_FAILED만 확정 상태로 반영
    - NOT_FOUND / UNKNOWN은 CLAIMED를 유지
    - 따라서 일시적인 API 지연/오류 때문에 재주문되지 않는다.
    """
    init_surge_execution_db()

    user_id = int(user_id)
    chat_id = int(chat_id or 0)
    message_id = int(message_id)

    result = dict(reconcile_result or {})

    state = str(
        result.get("state") or "UNKNOWN"
    ).upper()

    if state not in {
        "ACCEPTED",
        "FILLED",
        "TERMINAL_FAILED",
        "NOT_FOUND",
        "UNKNOWN",
    }:
        raise ValueError(
            f"Invalid reconcile state: {state}"
        )

    # NOT_FOUND와 UNKNOWN은 확정하지 않는다.
    if state in {"NOT_FOUND", "UNKNOWN"}:
        return {
            "updated": False,
            "status": "CLAIMED",
            "reconcile_state": state,
            "held": True,
        }

    if state == "TERMINAL_FAILED":
        db_status = "FAILED"
    else:
        db_status = state

    order_id = result.get("order_id")

    qty = result.get("cum_exec_qty")

    if qty in (None, ""):
        qty = result.get("qty")

    try:
        qty = (
            float(qty)
            if qty not in (None, "")
            else None
        )
    except Exception:
        qty = None

    error = None

    if state == "TERMINAL_FAILED":
        error = (
            "Bybit terminal order status: "
            f"{result.get('order_status')}"
        )

    with sqlite3.connect(SURGE_EXECUTION_DB) as con:
        cur = con.execute("""
            UPDATE surge_executions
            SET
                status=?,
                order_id=COALESCE(?, order_id),
                qty=COALESCE(?, qty),
                error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=?
              AND chat_id=?
              AND message_id=?
              AND status IN (
                  'CLAIMED',
                  'ACCEPTED'
              )
        """, (
            db_status,
            (
                str(order_id)
                if order_id not in (None, "")
                else None
            ),
            qty,
            error,
            user_id,
            chat_id,
            message_id,
        ))

        con.commit()

        updated = cur.rowcount == 1

    return {
        "updated": updated,
        "status": db_status,
        "reconcile_state": state,
        "held": False,
    }


def reconcile_surge_execution(
    user_id: int,
    chat_id: int,
    message_id: int,
):
    """
    surge_executions의 CLAIMED/ACCEPTED 한 건을
    orderLinkId로 Bybit와 대조하고 DB 상태를 갱신한다.

    주문하지 않는다.
    """
    execution = get_surge_execution(
        user_id,
        chat_id,
        message_id,
    )

    if execution is None:
        return {
            "found": False,
            "updated": False,
            "reason": "EXECUTION_NOT_FOUND",
        }

    order_link_id = execution.get(
        "order_link_id"
    )

    symbol = execution.get("symbol")

    result = reconcile_surge_order(
        user_id,
        order_link_id,
        symbol,
    )

    update = update_surge_execution_from_reconcile(
        user_id,
        chat_id,
        message_id,
        result,
    )

    return {
        "found": True,
        "execution": execution,
        "reconcile": result,
        "update": update,
    }


# ============================================================
# Surge market execution
# ============================================================

def execute_surge_market_order(
    user_id: int,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    *,
    entry_percent: float | None = None,
    dry_run: bool = True,
):
    """
    멍꿀단 급등매매 Market 진입 실행기.

    dry_run=True:
      - execution claim 하지 않음
      - 레버리지 변경하지 않음
      - 주문하지 않음

    dry_run=False:
      - READ-ONLY preview
      - execution CLAIM
      - 레버리지 적용
      - Available / 가격 / qty 최종 재계산
      - 안정성 재검증
      - Market 주문
      - 즉시 reconcile

    LONG:
      Buy / positionIdx=1

    SHORT:
      Sell / positionIdx=2
    """
    from get_session import get_session

    user_id = int(user_id)
    chat_id = int(chat_id or 0)
    message_id = int(message_id)

    side = str(side).upper().strip()

    if side == "LONG":
        order_side = "Buy"
        position_idx = 1

    elif side == "SHORT":
        order_side = "Sell"
        position_idx = 2

    else:
        raise ValueError(
            f"Invalid surge side: {side}"
        )

    # --------------------------------------------------------
    # 1. READ-ONLY preview
    # --------------------------------------------------------

    preview = build_surge_execution_preview(
        user_id,
        symbol,
        entry_percent=entry_percent,
    )

    if not preview.get("executable"):
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "executed": False,
            "reason": "PREVIEW_NOT_EXECUTABLE",
            "preview": preview,
        }

    final_plan = preview["final_plan"]

    trading_symbol = final_plan[
        "trading_symbol"
    ]

    order_link_id = surge_order_link_id(
        user_id,
        chat_id,
        message_id,
    )

    # --------------------------------------------------------
    # 2. DRY RUN
    #
    # DB / leverage / order 전부 무변경.
    # 실제 주문에 사용될 final_plan을 함께 반환한다.
    # --------------------------------------------------------

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "executed": False,
            "reason": "DRY_RUN",

            "user_id": user_id,
            "chat_id": chat_id,
            "message_id": message_id,

            "symbol": trading_symbol,
            "side": side,
            "order_side": order_side,
            "position_idx": position_idx,

            "order_link_id": order_link_id,

            "preview": preview,
            "final_plan": final_plan,
        }

    # --------------------------------------------------------
    # 3. execution CLAIM
    # --------------------------------------------------------

    claim = claim_surge_execution(
        user_id,
        chat_id,
        message_id,
        trading_symbol,
        side,
        signal_stage=final_plan.get(
            "signal_stage"
        ),
        entry_percent=final_plan.get(
            "entry_percent"
        ),
    )

    if not claim["claimed"]:
        existing = get_surge_execution(
            user_id,
            chat_id,
            message_id,
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "reason": "ALREADY_CLAIMED",
            "order_link_id": order_link_id,
            "claim": claim,
            "existing_execution": existing,
        }

    # --------------------------------------------------------
    # 4. 실제 레버리지 적용 + 최종 재계산
    #
    # prepare_surge_order_final(apply_leverage=True)가
    # 레버리지 변경 후 Available/Position/Price/qty를
    # 다시 계산한다.
    # --------------------------------------------------------

    try:
        prepared = prepare_surge_order_final(
            user_id,
            trading_symbol,
            entry_percent=entry_percent,
            apply_leverage=True,
        )

    except Exception as exc:
        fail_surge_execution(
            user_id,
            chat_id,
            message_id,
            (
                "FINAL_PREPARE_FAILED: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "reason": "FINAL_PREPARE_FAILED",
            "error":
                f"{type(exc).__name__}: {exc}",
            "claim": claim,
        }

    final_plan = prepared["final_plan"]

    stability = validate_surge_leverage_stability(
        prepared["initial_plan"],
        final_plan,
    )

    executable = (
        bool(final_plan["valid"])
        and bool(stability["stable"])
        and float(final_plan["qty"]) > 0
    )

    if not executable:
        fail_surge_execution(
            user_id,
            chat_id,
            message_id,
            (
                "FINAL_PLAN_NOT_EXECUTABLE: "
                f"{stability.get('reason')}"
            ),
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "reason": "FINAL_PLAN_NOT_EXECUTABLE",
            "claim": claim,
            "prepared": prepared,
            "stability": stability,
        }

    qty = float(final_plan["qty"])

    # --------------------------------------------------------
    # 5. Bybit session
    # --------------------------------------------------------

    session = get_session(user_id)

    if session is None:
        fail_surge_execution(
            user_id,
            chat_id,
            message_id,
            "BYBIT_SESSION_NOT_FOUND",
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "reason": "BYBIT_SESSION_NOT_FOUND",
        }

    payload = {
        "category": "linear",
        "symbol": trading_symbol,
        "side": order_side,
        "orderType": "Market",
        "qty": str(qty),
        "positionIdx": position_idx,
        "reduceOnly": False,
        "orderLinkId": order_link_id,
    }

    # --------------------------------------------------------
    # 6. Market 주문
    # --------------------------------------------------------

    try:
        response = session.place_order(
            **payload
        )

    except Exception as exc:
        # 네트워크/timeout 예외는 주문 미접수라고 단정할 수 없다.
        # FAILED 처리하지 않고 CLAIMED를 유지한 채 reconcile.
        reconcile = reconcile_surge_execution(
            user_id,
            chat_id,
            message_id,
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": True,
            "reason": "ORDER_EXCEPTION_RECONCILE",
            "error":
                f"{type(exc).__name__}: {exc}",
            "claim": claim,
            "prepared": prepared,
            "stability": stability,
            "payload": payload,
            "reconcile": reconcile,
        }

    # --------------------------------------------------------
    # 7. Bybit retCode 검증
    #
    # HTTP 호출 자체가 성공해도 retCode != 0이면
    # 거래소가 주문을 거절한 것.
    # ACCEPTED로 기록하면 안 된다.
    # --------------------------------------------------------

    if not isinstance(response, dict):
        # 응답 형태를 판정할 수 없으므로
        # 주문 접수 여부도 단정하지 않는다.
        reconcile = reconcile_surge_execution(
            user_id,
            chat_id,
            message_id,
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": True,
            "reason": "INVALID_ORDER_RESPONSE_RECONCILE",
            "claim": claim,
            "prepared": prepared,
            "stability": stability,
            "payload": payload,
            "response": response,
            "reconcile": reconcile,
        }

    ret_code = response.get("retCode")

    try:
        ret_code_int = int(ret_code)
    except (TypeError, ValueError):
        ret_code_int = None

    if ret_code_int != 0:
        ret_msg = str(
            response.get("retMsg") or ""
        )

        fail_surge_execution(
            user_id,
            chat_id,
            message_id,
            (
                "BYBIT_REJECTED: "
                f"retCode={ret_code!r} "
                f"retMsg={ret_msg}"
            ),
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": False,
            "reason": "BYBIT_REJECTED",
            "ret_code": ret_code,
            "ret_msg": ret_msg,
            "claim": claim,
            "prepared": prepared,
            "stability": stability,
            "payload": payload,
            "response": response,
        }

    # --------------------------------------------------------
    # 8. 정상 접수 응답
    # --------------------------------------------------------

    result = response.get("result") or {}

    order_id = result.get("orderId")

    # 정상 retCode인데 orderId가 없다면
    # ACCEPTED 확정하지 않고 reconcile한다.
    if not order_id:
        reconcile = reconcile_surge_execution(
            user_id,
            chat_id,
            message_id,
        )

        return {
            "ok": False,
            "dry_run": False,
            "executed": True,
            "reason": "ORDER_ID_MISSING_RECONCILE",
            "claim": claim,
            "prepared": prepared,
            "stability": stability,
            "payload": payload,
            "response": response,
            "reconcile": reconcile,
        }

    complete_surge_execution(
        user_id,
        chat_id,
        message_id,
        order_id=order_id,
        qty=qty,
        notional=final_plan.get(
            "final_notional"
        ),
        leverage=final_plan.get(
            "selected_leverage"
        ),
        risk_id=final_plan.get(
            "selected_risk_id"
        ),
        risk_limit=final_plan.get(
            "selected_risk_limit"
        ),
    )

    # --------------------------------------------------------
    # 9. 즉시 reconcile
    # --------------------------------------------------------

    reconcile = reconcile_surge_execution(
        user_id,
        chat_id,
        message_id,
    )

    return {
        "ok": True,
        "dry_run": False,
        "executed": True,
        "reason": "ORDER_SUBMITTED",
        "order_link_id": order_link_id,
        "claim": claim,
        "prepared": prepared,
        "stability": stability,
        "final_plan": final_plan,
        "payload": payload,
        "response": response,
        "reconcile": reconcile,
    }


# ============================================================
# Bybit Linear Instrument local cache
# ============================================================

INSTRUMENT_DB = BASE_DIR / "instruments.db"


def init_instrument_db():
    """
    Bybit USDT Linear instrument 영구 캐시.
    """
    with sqlite3.connect(INSTRUMENT_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS instruments (
                symbol TEXT PRIMARY KEY,

                status TEXT NOT NULL,

                min_order_qty REAL NOT NULL,
                qty_step REAL NOT NULL,
                max_market_qty REAL NOT NULL,
                max_order_qty REAL NOT NULL,
                min_notional REAL NOT NULL,

                tick_size REAL NOT NULL DEFAULT 0,
                max_leverage REAL NOT NULL DEFAULT 0,

                updated_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_instruments_status
            ON instruments(status)
        """)

        con.commit()


def get_cached_instrument(
    symbol: str,
):
    """
    instruments.db에서 단일 종목 조회.
    API 호출하지 않는다.
    """
    init_instrument_db()

    symbol = str(symbol).upper().strip()

    if not symbol.endswith("USDT"):
        symbol += "USDT"

    with sqlite3.connect(INSTRUMENT_DB) as con:
        con.row_factory = sqlite3.Row

        row = con.execute("""
            SELECT *
            FROM instruments
            WHERE symbol = ?
        """, (symbol,)).fetchone()

    return dict(row) if row else None


def get_cached_linear_symbols():
    """
    DB에 저장된 Trading 상태의 USDT Linear symbol 목록.
    API 호출하지 않는다.
    """
    init_instrument_db()

    with sqlite3.connect(INSTRUMENT_DB) as con:
        rows = con.execute("""
            SELECT symbol
            FROM instruments
            WHERE status = 'Trading'
              AND symbol LIKE '%USDT'
            ORDER BY symbol
        """).fetchall()

    return [
        str(row[0]).upper()
        for row in rows
    ]


def upsert_instrument_rows(rows):
    """
    Bybit instrument rows를 DB에 UPSERT.

    기존 DB 전체를 삭제하지 않는다.
    신규 종목은 추가하고 기존 종목의 규칙 변경은 갱신한다.
    """
    init_instrument_db()

    count = 0

    with sqlite3.connect(INSTRUMENT_DB) as con:
        for item in rows:
            symbol = str(
                item.get("symbol") or ""
            ).upper().strip()

            if not symbol.endswith("USDT"):
                continue

            lot = item.get(
                "lotSizeFilter"
            ) or {}

            price_filter = item.get(
                "priceFilter"
            ) or {}

            leverage_filter = item.get(
                "leverageFilter"
            ) or {}

            con.execute("""
                INSERT INTO instruments (
                    symbol,
                    status,
                    min_order_qty,
                    qty_step,
                    max_market_qty,
                    max_order_qty,
                    min_notional,
                    tick_size,
                    max_leverage,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(symbol)
                DO UPDATE SET
                    status = excluded.status,
                    min_order_qty =
                        excluded.min_order_qty,
                    qty_step =
                        excluded.qty_step,
                    max_market_qty =
                        excluded.max_market_qty,
                    max_order_qty =
                        excluded.max_order_qty,
                    min_notional =
                        excluded.min_notional,
                    tick_size =
                        excluded.tick_size,
                    max_leverage =
                        excluded.max_leverage,
                    updated_at =
                        CURRENT_TIMESTAMP
            """, (
                symbol,
                str(
                    item.get("status")
                    or ""
                ),
                float(
                    lot.get("minOrderQty")
                    or 0
                ),
                float(
                    lot.get("qtyStep")
                    or 0
                ),
                float(
                    lot.get("maxMktOrderQty")
                    or lot.get("maxOrderQty")
                    or 0
                ),
                float(
                    lot.get("maxOrderQty")
                    or 0
                ),
                float(
                    lot.get("minNotionalValue")
                    or 0
                ),
                float(
                    price_filter.get("tickSize")
                    or 0
                ),
                float(
                    leverage_filter.get(
                        "maxLeverage"
                    )
                    or 0
                ),
            ))

            count += 1

        con.commit()

    return count


def refresh_instrument_cache(
    user_id: int,
):
    """
    Bybit Linear instruments 전체 페이지를 읽어서
    instruments.db에 UPSERT.

    기존 행을 DELETE하지 않는다.
    """
    from get_session import get_session

    session = get_session(int(user_id))

    if session is None:
        raise RuntimeError(
            f"Bybit session not found: user_id={user_id}"
        )

    cursor = None
    api_rows = 0
    saved_rows = 0
    pages = 0

    while True:
        kwargs = {
            "category": "linear",
            "limit": 1000,
        }

        if cursor:
            kwargs["cursor"] = cursor

        result = session.get_instruments_info(
            **kwargs
        )

        data = result.get(
            "result",
            {}
        )

        rows = data.get(
            "list",
            []
        )

        pages += 1
        api_rows += len(rows)

        saved_rows += upsert_instrument_rows(
            rows
        )

        cursor = str(
            data.get("nextPageCursor")
            or ""
        ).strip()

        if not cursor:
            break

    return {
        "pages": pages,
        "api_rows": api_rows,
        "saved_rows": saved_rows,
        "cached_symbols": len(
            get_cached_linear_symbols()
        ),
    }


def resolve_surge_symbol_cached(
    user_id: int,
    raw_symbol: str,
    *,
    refresh_on_miss: bool = True,
):
    """
    Telegram 심볼 확정.

    1. DB exact match
    2. 정확히 마지막 한 글자만 빠진 유일 후보
    3. 둘 다 없으면 신규 상장 가능성이 있으므로
       API 전체 refresh 1회
    4. refresh 후 다시 판정
    5. 여전히 0개/복수 후보면 미확정

    반환 mode:
      EXACT
      RECOVERED_ONE_CHAR
      NO_MATCH
      AMBIGUOUS
      INVALID
    """

    raw = normalize_surge_symbol(
        raw_symbol
    )

    if not raw:
        return {
            "resolved": False,
            "raw_symbol": raw_symbol,
            "symbol": None,
            "trading_symbol": None,
            "mode": "INVALID",
            "candidates": [],
            "refreshed": False,
        }

    if raw.endswith("USDT"):
        base = raw[:-4]
    else:
        base = raw

    def lookup():
        symbols = get_cached_linear_symbols()

        requested = base + "USDT"

        if requested in symbols:
            return {
                "resolved": True,
                "raw_symbol": raw_symbol,
                "symbol": base,
                "trading_symbol": requested,
                "mode": "EXACT",
                "candidates": [requested],
            }

        candidates = []

        for trading_symbol in symbols:
            candidate_base = (
                trading_symbol[:-4]
            )

            if (
                candidate_base.startswith(base)
                and len(candidate_base)
                    == len(base) + 1
            ):
                candidates.append(
                    trading_symbol
                )

        candidates.sort()

        if len(candidates) == 1:
            trading_symbol = candidates[0]

            return {
                "resolved": True,
                "raw_symbol": raw_symbol,
                "symbol":
                    trading_symbol[:-4],
                "trading_symbol":
                    trading_symbol,
                "mode":
                    "RECOVERED_ONE_CHAR",
                "candidates":
                    candidates,
            }

        return {
            "resolved": False,
            "raw_symbol": raw_symbol,
            "symbol": None,
            "trading_symbol": None,
            "mode": (
                "AMBIGUOUS"
                if len(candidates) > 1
                else "NO_MATCH"
            ),
            "candidates": candidates,
        }

    first = lookup()

    if first["resolved"]:
        first["refreshed"] = False
        return first

    # AMBIGUOUS는 API refresh를 해도 추측하면 안 된다.
    if (
        first["mode"] == "AMBIGUOUS"
        or not refresh_on_miss
    ):
        first["refreshed"] = False
        return first

    # NO_MATCH만 신규상장 가능성을 고려하여
    # instruments API를 한 번 갱신한다.
    refresh_instrument_cache(
        user_id
    )

    second = lookup()
    second["refreshed"] = True

    return second


init_instrument_db()
