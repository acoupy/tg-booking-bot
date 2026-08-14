"""Переключение бота между режимами.

    python set_webhook.py https://ваш-проект.vercel.app/api/webhook   # включить вебхук
    python set_webhook.py --off                                       # вернуть polling
    python set_webhook.py --info                                      # что сейчас у Telegram
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv

from bot.app import build_bot


async def main(argv: list[str]) -> None:
    bot = build_bot()
    try:
        if not argv or argv[0] == "--info":
            info = await bot.get_webhook_info()
            print(f"url: {info.url or '(не задан, работает polling)'}")
            print(f"в очереди апдейтов: {info.pending_update_count}")
            if info.last_error_message:
                print(f"последняя ошибка: {info.last_error_message}")
            return

        if argv[0] == "--off":
            await bot.delete_webhook(drop_pending_updates=True)
            print("вебхук снят — можно запускать run_polling.py")
            return

        await bot.set_webhook(
            argv[0],
            secret_token=os.getenv("WEBHOOK_SECRET") or None,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        print(f"вебхук установлен: {argv[0]}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main(sys.argv[1:]))
