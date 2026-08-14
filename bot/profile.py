"""Витрина бота в Telegram: имя, описание, список команд, кнопка меню.

Это первое, что видит человек, открывший бота. Настройки те же, что руками
выставляются у @BotFather, но раз они лежат в коде и берутся из `config.json`, то
переезжают к следующему владельцу вместе с ботом.

Выставляются один раз при включении вебхука (`/api/setup`). Через Bot API не задаются
только две вещи: аватар и @username — это руками у @BotFather.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    MenuButtonCommands,
)

from . import texts
from .config import BookingConfig

log = logging.getLogger(__name__)

PUBLIC_COMMANDS = (BotCommand(command="start", description="Записаться"),)

ADMIN_COMMANDS = PUBLIC_COMMANDS + (
    BotCommand(command="today", description="Записи на сегодня"),
    BotCommand(command="week", description="Записи на неделю вперёд"),
    BotCommand(command="id", description="Мой Telegram ID"),
)


async def apply_profile(bot: Bot, config: BookingConfig, admins: set[int]) -> None:
    """Имя, описание и команды. Админам — свой список: клиенту сводка дня не нужна."""
    try:
        await bot.set_my_name(name=texts.bot_name(config))
    except Exception:  # имя меняется не чаще раза в сутки — отказ не должен ронять настройку
        log.warning("не удалось выставить имя бота", exc_info=True)
    await bot.set_my_short_description(short_description=texts.bot_short_description(config))
    await bot.set_my_description(description=texts.bot_description(config))
    await bot.set_my_commands(list(PUBLIC_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    for admin_id in sorted(admins):
        try:
            await bot.set_my_commands(
                list(ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:  # админ мог не начать диалог с ботом — не роняем настройку
            log.warning("не удалось выставить команды админу %s", admin_id, exc_info=True)
