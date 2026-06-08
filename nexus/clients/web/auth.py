"""Telegram Login Widget verification + dev-mode bypass.

The Login Widget redirects back to our callback with the user's id, name,
photo, auth_date, and an HMAC ``hash`` field. We verify the HMAC using
the bot token as the key. See https://core.telegram.org/widgets/login#checking-authorization
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any


class TelegramAuthError(Exception):
    pass


# A 24-hour validity window on the Telegram auth_date — past this we treat
# the widget data as expired and refuse the login.
_MAX_AUTH_AGE_SECONDS = 24 * 60 * 60


def verify_telegram_widget(bot_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the Telegram Login Widget callback payload.

    Returns the verified payload (minus ``hash``) on success. Raises
    ``TelegramAuthError`` on signature mismatch or expired auth_date.
    """
    data = {k: v for k, v in payload.items() if k != "hash"}
    received_hash = payload.get("hash")
    if not received_hash:
        raise TelegramAuthError("missing hash")

    # Build the data-check-string per Telegram's spec: <key>=<value> lines,
    # sorted alphabetically by key, joined with newlines.
    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise TelegramAuthError("hash mismatch")

    # Re-validate auth_date — Telegram suggests rejecting payloads older
    # than ~24h since they could be replays.
    try:
        auth_date = int(data["auth_date"])
    except (KeyError, ValueError) as exc:
        raise TelegramAuthError("invalid auth_date") from exc
    if auth_date < int(time.time()) - _MAX_AUTH_AGE_SECONDS:
        raise TelegramAuthError("auth_date expired")

    return data
