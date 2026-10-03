import asyncio

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from get_session import get_session


app = FastAPI()

USER_IDS = (2, 3)


def get_wallet_sync(user_id):
    session = get_session(user_id)

    return session.get_wallet_balance(
        accountType="UNIFIED",
    )


async def get_wallet(user_id):
    try:
        result = await asyncio.to_thread(
            get_wallet_sync,
            user_id,
        )

        wallet = result["result"]["list"][0]

        return {
            "user_id": user_id,
            "equity": wallet.get("totalEquity"),
            "wallet": wallet.get("totalWalletBalance"),
            "available": wallet.get("totalAvailableBalance"),
            "unrealised": wallet.get("totalPerpUPL"),
            "error": None,
        }

    except Exception as e:
        return {
            "user_id": user_id,
            "error": str(e),
        }


@app.get("/", response_class=HTMLResponse)
async def home():

    wallets = await asyncio.gather(
        *(get_wallet(uid) for uid in USER_IDS)
    )

    cards = ""

    for wallet in wallets:

        if wallet["error"]:
            cards += f"""
            <h2>User {wallet["user_id"]}</h2>
            <p>ERROR: {wallet["error"]}</p>
            """
            continue

        cards += f"""
        <h2>User {wallet["user_id"]}</h2>

        <p>Equity: {wallet["equity"]} USDT</p>
        <p>Wallet: {wallet["wallet"]} USDT</p>
        <p>Available: {wallet["available"]} USDT</p>
        <p>Unrealised PnL: {wallet["unrealised"]} USDT</p>

        <hr>
        """

    return f"""
<!doctype html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >
    <title>Closed PNL</title>
</head>

<body>

    <h1>Closed PNL</h1>

    {cards}

</body>
</html>
"""


