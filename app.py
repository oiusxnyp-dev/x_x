import asyncio
import os
import secrets
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from get_session import get_session
from user_db import authenticate_user, get_user_by_id


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "users.db"

load_dotenv(BASE_DIR / ".env")

app = FastAPI()

# 서버 재시작 때마다 로그인이 풀려도 괜찮은 현재 단계용.
# 이후 원하면 .env의 고정 SESSION_SECRET으로 바꿀 수 있음.
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="closed_pnl_session",
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=True,
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(
    directory=BASE_DIR / "templates"
)


def get_seed_percent(user_id: int) -> float:
    with sqlite3.connect(DB_FILE) as con:
        row = con.execute(
            """
            SELECT seed_percent
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        raise RuntimeError("user not found")

    return float(row[0])


def set_seed_percent(user_id: int, value: float) -> None:
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            """
            UPDATE users
            SET seed_percent = ?
            WHERE id = ?
            """,
            (value, user_id),
        )
        con.commit()


def get_wallet_sync(user_id: int):
    session = get_session(user_id)

    result = session.get_wallet_balance(
        accountType="UNIFIED",
    )

    wallet = result["result"]["list"][0]

    return {
        "equity": wallet.get("totalEquity"),
        "wallet": wallet.get("totalWalletBalance"),
        "available": wallet.get("totalAvailableBalance"),
        "unrealised": wallet.get("totalPerpUPL"),
    }


def current_user(request: Request):
    user_id = request.session.get("user_id")

    if not user_id:
        return None

    user = get_user_by_id(int(user_id))

    if not user:
        request.session.clear()
        return None

    if not int(user["approved"]) or not int(user["enabled"]):
        request.session.clear()
        return None

    return user


@app.get("/login")
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": None,
        },
    )


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = authenticate_user(
        username.strip(),
        password,
    )

    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "아이디 또는 비밀번호를 확인해주세요.",
            },
            status_code=401,
        )

    if not int(user["approved"]) or not int(user["enabled"]):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "사용할 수 없는 계정입니다.",
            },
            status_code=403,
        )

    request.session.clear()
    request.session["user_id"] = int(user["id"])

    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()

    return RedirectResponse(
        "/login",
        status_code=303,
    )


@app.get("/")
async def dashboard(request: Request):
    user = current_user(request)

    if not user:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    user_id = int(user["id"])

    try:
        wallet = await asyncio.to_thread(
            get_wallet_sync,
            user_id,
        )
        wallet_error = None

    except Exception as exc:
        wallet = None
        wallet_error = str(exc)

    seed_percent = get_seed_percent(user_id)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "wallet": wallet,
            "wallet_error": wallet_error,
            "seed_percent": seed_percent,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/settings/seed")
async def save_seed(
    request: Request,
    seed_percent: float = Form(...),
):
    user = current_user(request)

    if not user:
        return RedirectResponse(
            "/login",
            status_code=303,
        )

    # 실수로 음수/비정상적으로 큰 값을 저장하지 않도록 제한.
    if seed_percent < 0 or seed_percent > 100:
        return RedirectResponse(
            "/?saved=invalid",
            status_code=303,
        )

    set_seed_percent(
        int(user["id"]),
        seed_percent,
    )

    return RedirectResponse(
        "/?saved=1",
        status_code=303,
    )
