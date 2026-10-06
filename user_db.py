import sqlite3
import hashlib
from contextlib import closing
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
import secrets

from pathlib import Path
from datetime import datetime


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "users.db"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()


_password_hasher = PasswordHasher()


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(32)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        600000,
    ).hex()

    return password_hash, salt


def create_user(username, password):
    username = username.strip()

    if len(username) < 3:
        return False, "아이디는 3자 이상이어야 합니다."

    if len(password) < 8:
        return False, "비밀번호는 8자 이상이어야 합니다."

    # New accounts use Argon2id.
    # Keep a non-null compatibility value in the legacy salt column.
    password_hash = _password_hasher.hash(password)
    salt = "argon2"

    try:
        with closing(get_db()) as conn, conn:
            conn.execute(
                """
                INSERT INTO users (
                    username,
                    password_hash,
                    salt,
                    approved,
                    enabled,
                    created_at
                )
                VALUES (?, ?, ?, 0, 1, ?)
                """,
                (
                    username,
                    password_hash,
                    salt,
                    datetime.now().isoformat(
                        timespec="seconds"
                    ),
                ),
            )

            conn.commit()

        return True, "회원가입이 완료되었습니다."

    except sqlite3.IntegrityError:
        return False, "이미 사용 중인 아이디입니다."


def authenticate_user(username, password):
    with closing(get_db()) as conn, conn:
        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (username.strip(),),
        ).fetchone()

    if user is None:
        return None

    stored_hash = user["password_hash"]

    if stored_hash.startswith("$argon2"):
        try:
            if not _password_hasher.verify(stored_hash, password):
                return None
        except (VerifyMismatchError, VerificationError):
            return None

        if _password_hasher.check_needs_rehash(stored_hash):
            new_hash = _password_hasher.hash(password)
            with closing(get_db()) as conn, conn:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?
                    WHERE id = ?
                    """,
                    (new_hash, user["id"]),
                )
                conn.commit()

    else:
        password_hash, _ = hash_password(
            password,
            user["salt"],
        )

        if not secrets.compare_digest(
            password_hash,
            stored_hash,
        ):
            return None

        # Successful legacy PBKDF2 login:
        # migrate this account to Argon2id.
        new_hash = _password_hasher.hash(password)

        with closing(get_db()) as conn, conn:
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?
                WHERE id = ?
                  AND password_hash = ?
                """,
                (new_hash, user["id"], stored_hash),
            )
            conn.commit()

    return dict(user)

def get_user_by_id(user_id):
    with closing(get_db()) as conn, conn:
        user = conn.execute(
            """
            SELECT
                id,
                username,
                approved,
                enabled,
                created_at
            FROM users
            WHERE id = ?
            """,
            (int(user_id),),
        ).fetchone()

    if user is None:
        return None

    return dict(user)


def get_users():
    with closing(get_db()) as conn, conn:
        rows = conn.execute(
            """
            SELECT
                id,
                username,
                approved,
                enabled,
                created_at,
                exchange
            FROM users
            ORDER BY id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def approve_user(user_id):
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            UPDATE users
            SET approved = 1
            WHERE id = ?
            """,
            (int(user_id),),
        )

        conn.commit()


def set_user_enabled(user_id, enabled):
    with closing(get_db()) as conn, conn:
        conn.execute(
            """
            UPDATE users
            SET enabled = ?
            WHERE id = ?
            """,
            (
                1 if enabled else 0,
                int(user_id),
            ),
        )

        conn.commit()