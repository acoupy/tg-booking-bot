"""Сборка бота: одна и та же связка работает и в polling, и в вебхуке."""

from __future__ import annotations

import os
from functools import lru_cache

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .handlers import router


def build_bot(token: str | None = None) -> Bot:
    token = token or os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("не задан BOT_TOKEN (см. .env.example)")
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@lru_cache(maxsize=1)
def get_dispatcher() -> Dispatcher:
    """Один диспетчер на процесс.

    Роутер в aiogram привязывается к диспетчеру ровно один раз, а Vercel переиспользует
    «тёплый» контейнер между апдейтами — поэтому собираем его лениво и кэшируем.
    Конфиг и хранилище сюда не зашиты: они передаются вместе с апдейтом и попадают в
    хендлеры как обычные аргументы.
    """
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
