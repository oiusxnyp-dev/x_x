import sqlite3
from pathlib import Path
from contextlib import closing

from cryptography.fernet import Fernet
from dotenv import load_dotenv
import os


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "users.db"

load_dotenv()

key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")

if not key:
    raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is missing")

cipher = Fernet(key.encode())


def get_bybit_credentials(user_id):
    with closing(sqlite3.connect(DB_FILE)) as con, con:
        con.row_factory = sqlite3.Row

        row = con.execute("""
            SELECT api_key_encrypted, api_secret_encrypted
            FROM bybit_credentials
            WHERE user_id = ?
        """, (int(user_id),)).fetchone()

    if row is None:
        return None

    return {
        "api_key": cipher.decrypt(
            row["api_key_encrypted"].encode()
        ).decode(),
        "api_secret": cipher.decrypt(
            row["api_secret_encrypted"].encode()
        ).decode(),
    }