"""Витрина бота: описание и команды выставляются при включении вебхука.

Ошибиться здесь легко и заметно не сразу — заказчик просто увидит пустой профиль.
Поэтому прогоняем настройку на заглушке сессии и смотрим, что ушло в Telegram.
"""

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod

from bot.config import get_config
from bot.profile import apply_profile


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def close(self):
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - не используется
        yield b""

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        self.calls.append((type(method).__name__, method.model_dump(exclude_none=True)))
        return True

    def call(self, name: str, **where) -> dict | None:
        for method, payload in self.calls:
            if method == name and all(payload.get(k) == v for k, v in where.items()):
                return payload
        return None


@pytest.fixture
def bot():
    return Bot("42:TEST", session=RecordingSession())


async def test_profile_is_filled_in(bot):
    config = get_config()
    await apply_profile(bot, config, {777})

    assert bot.session.call("SetMyName")["name"] == config.title
    assert bot.session.call("SetMyShortDescription")
    assert config.title in bot.session.call("SetMyDescription")["description"]
    assert bot.session.call("SetChatMenuButton")

    public = bot.session.call("SetMyCommands", scope={"type": "all_private_chats"})
    assert [c["command"] for c in public["commands"]] == ["start"]

    # Админу — свой набор: сводка дня клиенту в меню команд не нужна.
    admin = bot.session.call("SetMyCommands", scope={"type": "chat", "chat_id": 777})
    assert "today" in [c["command"] for c in admin["commands"]]


async def test_admin_without_dialog_does_not_break_setup(bot, monkeypatch):
    """Админ мог не начать диалог с ботом — Telegram ответит ошибкой, настройка идёт дальше."""
    original = bot.session.make_request

    async def fail_for_admin(bot_, method, timeout=None):
        payload = method.model_dump(exclude_none=True)
        if type(method).__name__ == "SetMyCommands" and payload.get("scope", {}).get("chat_id"):
            raise RuntimeError("chat not found")
        return await original(bot_, method, timeout)

    monkeypatch.setattr(bot.session, "make_request", fail_for_admin)
    await apply_profile(bot, get_config(), {777})
    assert bot.session.call("SetMyDescription")
