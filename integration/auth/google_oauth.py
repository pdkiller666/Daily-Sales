"""Google OAuth 2.0 Device Flow — headless authorization for Telegram bots."""
import logging
import os
import time

import aiohttp

logger = logging.getLogger(__name__)

DEVICE_AUTH_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/spreadsheets"


def get_client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from environment variables."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError(
            "Секреты GOOGLE_OAUTH_CLIENT_ID и GOOGLE_OAUTH_CLIENT_SECRET не заданы. "
            "Обратитесь к администратору бота."
        )
    return client_id, client_secret


async def initiate_device_flow() -> dict:
    """
    Start Device Flow.
    Returns dict with: device_code, user_code, verification_url, expires_in, interval.
    """
    client_id, _ = get_client_credentials()
    async with aiohttp.ClientSession() as session:
        async with session.post(DEVICE_AUTH_URL, data={
            "client_id": client_id,
            "scope": SCOPES,
        }) as resp:
            data = await resp.json(content_type=None)
    if "error" in data:
        raise ValueError(f"Ошибка запуска OAuth: {data.get('error_description', data)}")
    return data


async def poll_for_token(device_code: str) -> dict | None:
    """
    Poll once for access token.
    Returns token dict on success, None if still pending.
    Raises ValueError on unrecoverable errors.
    """
    client_id, client_secret = get_client_credentials()
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }) as resp:
            data = await resp.json(content_type=None)

    if "access_token" in data:
        data["expiry"] = time.time() + data.get("expires_in", 3600)
        return data

    error = data.get("error", "")
    if error in ("authorization_pending", "slow_down"):
        return None

    raise ValueError(f"OAuth ошибка авторизации: {data.get('error_description', error)}")


async def refresh_access_token(refresh_token_str: str) -> dict:
    """
    Refresh expired access token using refresh_token.
    Returns new token dict with updated expiry.
    """
    client_id, client_secret = get_client_credentials()
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token_str,
            "grant_type": "refresh_token",
        }) as resp:
            data = await resp.json(content_type=None)

    if "access_token" not in data:
        raise ValueError(
            f"Не удалось обновить токен: {data.get('error_description', data.get('error', ''))}"
        )
    data["expiry"] = time.time() + data.get("expires_in", 3600)
    return data
