import asyncio
import os
import secrets
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

from get_session import get_session
from bybit_ws import ensure_bybit_realtime
from realtime import realtime
from user_db import authenticate_user, get_user_by_id
from surge_trading import (
    get_settings as get_surge_settings,
    save_settings as save_surge_settings,
    get_stage_settings,
    save_stage_entry_setting,
    get_surge_trailing_settings,
    save_surge_trailing_settings,
    get_surge_symbol_entry_plan,
    save_symbol_entry_percent,
    save_surge_auto_trading,
)


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

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "wallet": wallet,
            "wallet_error": wallet_error,
        },
    )


@app.get("/api/surge/settings")
async def surge_settings_api(request: Request):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    user_id = int(user["id"])

    try:
        settings = get_surge_settings(user_id)
        stages = get_stage_settings(user_id)
        trailing = get_surge_trailing_settings(
            user_id
        )
        symbols = get_surge_symbol_entry_plan(
            user_id
        )

        # 급등매매의 진입 비중은 증거금 비중이 아니라
        # 현재 Available 대비 주문 명목가치 비중이다.
        #
        # 종목마다 Bybit REST를 호출하지 않고
        # wallet Available을 한 번만 조회한 뒤
        # 각 종목의 다음 진입 비중으로 예상 명목가치를 계산한다.
        session = get_session(user_id)

        surge_available = None

        if session is not None:
            wallet_result = session.get_wallet_balance(
                accountType="UNIFIED",
            )

            wallet_rows = (
                wallet_result
                .get("result", {})
                .get("list", [])
            )

            if wallet_rows:
                surge_available = float(
                    wallet_rows[0].get(
                        "totalAvailableBalance"
                    ) or 0
                )

        for symbol_row in symbols:
            percent = float(
                symbol_row.get(
                    "next_entry_percent"
                ) or 0
            )

            symbol_row["available"] = surge_available

            if surge_available is None:
                symbol_row[
                    "next_entry_notional"
                ] = None
            else:
                symbol_row[
                    "next_entry_notional"
                ] = (
                    surge_available
                    * percent
                    / 100.0
                )

        return {
            "ok": True,
            "settings": settings,
            "stages": stages,
            "trailing": trailing,
            "symbols": symbols,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.post("/api/surge/global")
async def surge_global_api(
    request: Request,
    entry_percent: float = Form(...),
):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    if entry_percent < 0 or entry_percent > 1000:
        return {
            "ok": False,
            "error": "entry_percent must be 0..1000",
        }

    user_id = int(user["id"])

    try:
        current = get_surge_settings(user_id)

        save_surge_settings(
            user_id,
            enabled=bool(current["enabled"]),
            entry_percent=entry_percent,
        )

        return {
            "ok": True,
            "entry_percent": float(
                entry_percent
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.post("/api/surge/enabled")
async def surge_enabled_api(
    request: Request,
    enabled: int = Form(...),
):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    if enabled not in (0, 1):
        return {
            "ok": False,
            "error": "enabled must be 0 or 1",
        }

    user_id = int(user["id"])

    try:
        result = save_surge_auto_trading(
            user_id,
            bool(enabled),
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "error": (
                    result.get("message")
                    or result.get("reason")
                    or "surge trading blocked"
                ),
                **result,
            }

        return result

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.post("/api/surge/stage")
async def surge_stage_api(
    request: Request,
    stage: int = Form(...),
    entry_percent: float = Form(...),
    use_global: int = Form(0),
):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    if stage < 1:
        return {
            "ok": False,
            "error": "stage must be >= 1",
        }

    if entry_percent < 0 or entry_percent > 1000:
        return {
            "ok": False,
            "error": "entry_percent must be 0..1000",
        }

    if use_global not in (0, 1):
        return {
            "ok": False,
            "error": "use_global must be 0 or 1",
        }

    user_id = int(user["id"])

    try:
        save_stage_entry_setting(
            user_id,
            stage,
            entry_percent=entry_percent,
            use_global=bool(use_global),
        )

        return {
            "ok": True,
            "stage": int(stage),
            "entry_percent": float(
                entry_percent
            ),
            "use_global": bool(
                use_global
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.post("/api/surge/symbol")
async def surge_symbol_api(
    request: Request,
    symbol: str = Form(...),
    entry_percent: str = Form(""),
):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    user_id = int(user["id"])
    symbol = symbol.strip().upper()

    if not symbol:
        return {
            "ok": False,
            "error": "symbol required",
        }

    try:
        if entry_percent.strip() == "":
            value = None
        else:
            value = float(entry_percent)

            if value < 0 or value > 1000:
                return {
                    "ok": False,
                    "error":
                        "entry_percent must be 0..1000",
                }

        saved = save_symbol_entry_percent(
            user_id,
            symbol,
            value,
        )

        return {
            "ok": True,
            "symbol": symbol,
            "entry_percent": saved,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.post("/api/surge/trailing")
async def surge_trailing_api(
    request: Request,
    arm_percent: float = Form(...),
    gap_percent: float = Form(...),
):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    user_id = int(user["id"])

    try:
        saved = save_surge_trailing_settings(
            user_id,
            arm_percent,
            gap_percent,
        )

        return {
            "ok": True,
            **saved,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.get("/api/wallet")
async def wallet_api(request: Request):
    user = current_user(request)

    if not user:
        return {
            "ok": False,
            "error": "not_authenticated",
        }

    try:
        wallet = await asyncio.to_thread(
            get_wallet_sync,
            int(user["id"]),
        )

        return {
            "ok": True,
            **wallet,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


@app.websocket("/ws")
async def realtime_websocket(websocket: WebSocket):
    user_id = websocket.session.get("user_id")

    if not user_id:
        await websocket.close(code=1008)
        return

    user = get_user_by_id(int(user_id))

    if (
        not user
        or not int(user["approved"])
        or not int(user["enabled"])
    ):
        await websocket.close(code=1008)
        return

    user_id = int(user["id"])

    try:
        await asyncio.to_thread(
            ensure_bybit_realtime,
            user_id,
        )
    except Exception as exc:
        print(
            "[WEB WS] realtime start failed "
            f"user_id={user_id} error={exc}",
            flush=True,
        )
        await websocket.close(code=1011)
        return

    await websocket.accept()

    print(
        f"[WEB WS] connected user_id={user_id}",
        flush=True,
    )

    try:
        while True:
            snapshot = realtime.snapshot(user_id)

            await websocket.send_json({
                "type": "realtime",
                **snapshot,
            })

            # ticker는 내부에서 ~100ms 수준으로 갱신된다.
            # 브라우저에도 최대 10Hz로 전달.
            await asyncio.sleep(0.1)

    except WebSocketDisconnect:
        print(
            f"[WEB WS] disconnected user_id={user_id}",
            flush=True,
        )

    except Exception as exc:
        print(
            "[WEB WS] error "
            f"user_id={user_id} error={exc}",
            flush=True,
        )
