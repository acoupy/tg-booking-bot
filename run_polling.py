"""Локальный запуск: бот сам опрашивает Telegram, вебхук и хостинг не нужны.

    python run_polling.py

Записи в этом режиме складываются в data/bookings.json.
"""

from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv

from bot.app import build_bot, get_dispatcher
from bot.config import get_config
from bot.storage import create_store


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    bot = build_bot()
    store = create_store()

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    logging.info("бот @%s запущен в режиме polling", me.username)
    try:
        await get_dispatcher().start_polling(bot, config=get_config(), store=store)
    finally:
        await store.close()
        await bot.session.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
