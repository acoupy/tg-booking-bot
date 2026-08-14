"""Точка входа на Vercel.

Vercel ищет точку входа в файле со «стандартным» именем (`app.py`, `index.py`,
`main.py`…) и загружает из него переменную `app`. Поэтому оба адреса — вебхук
Telegram и утренний cron — обслуживает одно маленькое ASGI-приложение без фреймворка:

* `POST /api/webhook` — апдейты от Telegram;
* `GET  /api/cron`    — утренний прогон: напоминания и уборка, по расписанию из vercel.json;
* `GET  /api/setup`   — разовая настройка: бот прописывает свой адрес и витрину в Telegram;
* `GET  /api/webhook` — проверка «жив ли эндпоинт» из браузера.

Функция просыпается на каждый запрос и засыпает: ни фонового цикла, ни памяти между
вызовами здесь нет — состояние диалога живёт в кнопках, данные в хранилище.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import datetime
from urllib.parse import parse_qs

from aiogram.types import Update

from bot.app import build_bot, get_dispatcher
from bot.booking import day_bookings, purge_expired
from bot.config import admin_ids, get_config
from bot.profile import apply_profile
from bot.storage import Store, create_store
from bot import texts

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

SEEN_TTL = 60 * 60  # столько помним номера обработанных апдейтов


def describe(payload: dict) -> str:
    """Строка для лога: по ней в Runtime Logs видно, что и в каком порядке пришло."""
    message = payload.get("message") or {}
    callback = payload.get("callback_query") or {}
    sender = (message.get("from") or callback.get("from") or {}).get("id")
    what = (
        message.get("text")
        or callback.get("data")
        or ("контакт" if message.get("contact") else "")
        or "без текста"
    )
    return f"апдейт {payload.get('update_id')} от {sender}: {what}"


async def already_handled(store: Store, update_id: int | None) -> bool:
    """Telegram повторяет апдейт, если не дождался ответа за минуту.

    На холодном старте функция как раз может не уложиться, и тогда одно и то же
    нажатие приезжает дважды — с ответом невпопад, а то и со второй записью.
    Номер апдейта разовый, поэтому первым делом занимаем его через SETNX.
    """
    if update_id is None:
        return False
    return not await store.set_nx(f"seen:{update_id}", "1", ttl=SEEN_TTL)


async def handle_update(payload: dict) -> None:
    store = create_store()
    log.info("%s", describe(payload))
    try:
        if await already_handled(store, payload.get("update_id")):
            log.info("апдейт %s уже обработан — пропускаю повтор", payload.get("update_id"))
            return
        bot = build_bot()
        try:
            await get_dispatcher().feed_update(
                bot,
                Update.model_validate(payload, context={"bot": bot}),
                config=get_config(),
                store=store,
            )
        finally:
            await bot.session.close()
    finally:
        await store.close()


async def morning_job() -> tuple[int, int]:
    """Утренний прогон: напоминания на сегодня и уборка вышедших из хранения дней."""
    config = get_config()
    bot = build_bot()
    store = create_store()
    sent = 0
    try:
        now = datetime.now(config.tz)
        for booking in await day_bookings(store, now.date()):
            try:
                await bot.send_message(booking.user_id, texts.reminder(config, booking))
                sent += 1
            except Exception:  # клиент мог заблокировать бота — не роняем рассылку
                log.warning("не доставлено напоминание %s", booking.id, exc_info=True)
        purged = await purge_expired(store, config, now)
    finally:
        await store.close()
        await bot.session.close()
    log.info("утренний прогон: напоминаний %s, убрано записей %s", sent, purged)
    return sent, purged


async def setup_webhook(host: str) -> str:
    """Включает вебхук и заодно приводит в порядок витрину бота в Telegram."""
    url = f"https://{host}/api/webhook"
    bot = build_bot()
    try:
        await bot.set_webhook(
            url,
            secret_token=os.getenv("WEBHOOK_SECRET") or None,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        await apply_profile(bot, get_config(), admin_ids())
        me = await bot.get_me()
        return (
            f"вебхук включён: {url}\n"
            f"описание, команды и меню бота @{me.username} обновлены — готов принимать сообщения"
        )
    finally:
        await bot.session.close()


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            return body


async def _respond(send, status: int, text: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": text.encode("utf-8")})


async def app(scope, receive, send) -> None:
    if scope["type"] != "http":
        return

    path = scope["path"].rstrip("/")
    method = scope["method"]
    headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}

    if path == "/api/setup":
        secret = os.getenv("WEBHOOK_SECRET")
        given = parse_qs(scope.get("query_string", b"").decode()).get("key", [""])[0]
        if secret and given != secret:
            await _respond(send, 403, "forbidden")
            return
        try:
            await _respond(send, 200, await setup_webhook(headers.get("host", "")))
        except Exception as error:
            await _respond(send, 500, f"не вышло: {type(error).__name__}: {error}")
        return

    if path == "/api/cron":
        secret = os.getenv("CRON_SECRET")
        if secret and headers.get("authorization") != f"Bearer {secret}":
            await _respond(send, 403, "forbidden")
            return
        sent, purged = await morning_job()
        await _respond(send, 200, f"reminders sent: {sent}, purged bookings: {purged}")
        return

    if method != "POST":
        await _respond(send, 200, "bot webhook is up")
        return

    secret = os.getenv("WEBHOOK_SECRET")
    if secret and headers.get("x-telegram-bot-api-secret-token") != secret:
        await _respond(send, 403, "forbidden")
        return

    try:
        payload = json.loads((await _read_body(receive)).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        await _respond(send, 400, "bad json")
        return

    try:
        await handle_update(payload)
    except Exception:
        # Сюда долетает только то, что упало вне диалога (сборка бота, хранилище):
        # ошибки внутри хендлеров перехватывает `on_error` и отвечает клиенту сам.
        # Отвечаем 200 в любом случае: иначе Telegram будет слать этот апдейт по кругу.
        traceback.print_exc()
    await _respond(send, 200, "ok")
