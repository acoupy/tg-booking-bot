"""Проверка точки входа: маршруты, отсечение чужих запросов и повторов.

Реальную обработку апдейта тут не гоняем — она покрыта test_dialog. Здесь важно,
что вебхук не пускает запросы без секрета и не падает на мусорном теле.
"""

import pytest

from app import already_handled, app, describe
from bot.storage import JsonStore


async def call(
    method: str, path: str, body: bytes = b"", headers: dict | None = None, query: str = ""
):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return messages.pop(0)

    sent = []

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = sent[0]["status"]
    text = sent[1]["body"].decode("utf-8")
    return status, text


async def test_get_reports_alive():
    status, text = await call("GET", "/api/webhook")
    assert status == 200
    assert text == "bot webhook is up"


async def test_webhook_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    status, text = await call(
        "POST", "/api/webhook", b"{}", {"X-Telegram-Bot-Api-Secret-Token": "guess"}
    )
    assert status == 403
    assert text == "forbidden"


async def test_webhook_survives_broken_body(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    status, text = await call(
        "POST", "/api/webhook", b"not json", {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    )
    assert status == 400
    assert text == "bad json"


async def test_setup_requires_key(monkeypatch):
    """Адрес включения вебхука не должен срабатывать у случайного прохожего."""
    monkeypatch.setenv("WEBHOOK_SECRET", "s3cret")
    status, text = await call("GET", "/api/setup", query="key=guess")
    assert status == 403
    assert text == "forbidden"


async def test_cron_requires_authorization(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-key")
    status, text = await call("GET", "/api/cron")
    assert status == 403
    assert text == "forbidden"


async def test_repeated_update_is_ignored(tmp_path):
    """Не дождавшись ответа за минуту, Telegram шлёт апдейт заново — обработать надо один раз."""
    store = JsonStore(tmp_path / "bookings.json")
    assert await already_handled(store, 500) is False
    assert await already_handled(store, 500) is True
    assert await already_handled(store, 501) is False


async def test_log_line_shows_what_arrived():
    """Строка лога должна отвечать на вопрос «что и в каком порядке приехало»."""
    line = describe({"update_id": 7, "message": {"from": {"id": 777}, "text": "/start"}})
    assert "7" in line and "777" in line and "/start" in line

    pressed = describe({"update_id": 8, "callback_query": {"from": {"id": 777}, "data": "menu:my"}})
    assert "8" in pressed and "menu:my" in pressed
