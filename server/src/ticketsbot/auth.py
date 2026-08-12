from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramUser:
    id: str
    name: str
    username: str = ""
    photo_url: str = ""


def verify_init_data(
    init_data: str | None,
    bot_token: str,
    max_age_seconds: int = 24 * 60 * 60,
    future_skew_seconds: int = 60,
    now: int | None = None,
) -> TelegramUser:
    if not bot_token:
        raise AuthError("Сервер не настроен: отсутствует BOT_TOKEN.")
    if not init_data:
        raise AuthError("Откройте бота в официальном приложении Telegram.")
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    supplied_hash = fields.pop("hash", "")
    if not supplied_hash:
        raise AuthError("Некорректные данные авторизации.")
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise AuthError("Проверка подписи Telegram не пройдена.")
    try:
        auth_date = int(fields.get("auth_date", ""))
    except (TypeError, ValueError):
        raise AuthError("Некорректное время авторизации Telegram.")
    current = int(time.time()) if now is None else int(now)
    if auth_date > current + future_skew_seconds:
        raise AuthError("Время авторизации Telegram находится в будущем.")
    if auth_date < current - max_age_seconds:
        raise AuthError("Сессия Telegram устарела. Откройте приложение заново.")
    try:
        raw = json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError:
        raw = {}
    if not raw.get("id"):
        raise AuthError("В данных авторизации нет пользователя.")
    return TelegramUser(
        id=str(raw["id"]),
        name=" ".join(x for x in (raw.get("first_name"), raw.get("last_name")) if x),
        username=raw.get("username", ""),
        photo_url=raw.get("photo_url", ""),
    )
