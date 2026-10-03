from pybit.unified_trading import HTTP
from bybit_credentials import get_bybit_credentials


def get_session(user_id):
    credentials = get_bybit_credentials(int(user_id))

    if not credentials:
        return None

    return HTTP(
        testnet=False,
        api_key=credentials["api_key"],
        api_secret=credentials["api_secret"],
    )