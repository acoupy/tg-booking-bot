"""Клавиатуры и разбор callback-данных.

Весь путь клиента (услуга → специалист → день → время) закодирован прямо в кнопках.
Это не украшение: на serverless-вебхуке нет процесса, который помнил бы, на каком
шаге находится диалог, — кнопка сама несёт весь контекст.
"""

from __future__ import annotations

from datetime import date

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import BookingConfig, Service
from .schedule import fmt_date, minutes_of, parse_hhmm

ANY_SPECIALIST = "any"


class MenuCB(CallbackData, prefix="menu"):
    action: str  # root | book | my | info


class ServiceCB(CallbackData, prefix="srv"):
    service: str


class SpecialistCB(CallbackData, prefix="spc"):
    service: str
    specialist: str


class DayCB(CallbackData, prefix="day"):
    service: str
    specialist: str
    day: str


class TimeCB(CallbackData, prefix="tm"):
    service: str
    specialist: str
    day: str
    start: int  # минуты от полуночи: «10:30» в callback-данные не влезает, там ':' — разделитель


class ConfirmCB(CallbackData, prefix="ok"):
    pass


class CancelCB(CallbackData, prefix="cnl"):
    booking: str


class MoveCB(CallbackData, prefix="mv"):
    """Перенос: услугу и специалиста берём из самой записи, в кнопке их нести незачем."""

    booking: str


class MoveDayCB(CallbackData, prefix="mvd"):
    booking: str
    day: str


class MoveTimeCB(CallbackData, prefix="mvt"):
    booking: str
    day: str
    start: int


class MoveOkCB(CallbackData, prefix="mvok"):
    booking: str
    day: str
    start: int


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data=MenuCB(action="book"))
    kb.button(text="🗓 Мои записи", callback_data=MenuCB(action="my"))
    kb.button(text="ℹ️ Как это работает", callback_data=MenuCB(action="info"))
    kb.adjust(1)
    if is_admin:
        kb.row(InlineKeyboardButton(text="⚙️ Записи на сегодня", callback_data="admin:today"))
    return kb.as_markup()


def services_kb(config: BookingConfig) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for service in config.services:
        price = f" — {service.price_label}" if service.price_label else ""
        kb.button(text=f"{service.title}{price}", callback_data=ServiceCB(service=service.id))
    kb.button(text="‹ Назад", callback_data=MenuCB(action="root"))
    kb.adjust(1)
    return kb.as_markup()


def specialists_kb(config: BookingConfig, service: Service) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎲 Любой специалист — больше свободных окон",
        callback_data=SpecialistCB(service=service.id, specialist=ANY_SPECIALIST),
    )
    for specialist in config.specialists_for(service.id):
        kb.button(
            text=specialist.name,
            callback_data=SpecialistCB(service=service.id, specialist=specialist.id),
        )
    kb.button(text="‹ К услугам", callback_data=MenuCB(action="book"))
    kb.adjust(1)
    return kb.as_markup()


def days_kb(service_id: str, specialist_id: str, days: list[date]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for day in days:
        kb.button(
            text=fmt_date(day),
            callback_data=DayCB(service=service_id, specialist=specialist_id, day=day.isoformat()),
        )
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="‹ К специалистам", callback_data=ServiceCB(service=service_id).pack()
        )
    )
    return kb.as_markup()


def times_kb(
    service_id: str, specialist_id: str, day: date, starts: list[str]
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for start in starts:
        kb.button(
            text=start,
            callback_data=TimeCB(
                service=service_id,
                specialist=specialist_id,
                day=day.isoformat(),
                start=minutes_of(parse_hhmm(start)),
            ),
        )
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text="‹ К датам",
            callback_data=SpecialistCB(service=service_id, specialist=specialist_id).pack(),
        )
    )
    return kb.as_markup()


def confirm_kb(service_id: str, specialist_id: str, day: date) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить запись", callback_data=ConfirmCB())
    kb.button(
        text="‹ Выбрать другое время",
        callback_data=DayCB(service=service_id, specialist=specialist_id, day=day.isoformat()),
    )
    kb.adjust(1)
    return kb.as_markup()


def my_bookings_kb(booking_ids: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    single = len(booking_ids) == 1
    for index, booking_id in enumerate(booking_ids, start=1):
        number = "" if single else f" {index}"  # с одной записью номер только мешает
        kb.row(
            InlineKeyboardButton(
                text=f"🔁 Перенести{number}", callback_data=MoveCB(booking=booking_id).pack()
            ),
            InlineKeyboardButton(
                text=f"❌ Отменить{number}", callback_data=CancelCB(booking=booking_id).pack()
            ),
        )
    kb.row(InlineKeyboardButton(text="‹ В меню", callback_data=MenuCB(action="root").pack()))
    return kb.as_markup()


def move_days_kb(booking_id: str, days: list[date]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for day in days:
        kb.button(
            text=fmt_date(day),
            callback_data=MoveDayCB(booking=booking_id, day=day.isoformat()),
        )
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="‹ К моим записям", callback_data=MenuCB(action="my").pack()))
    return kb.as_markup()


def move_times_kb(booking_id: str, day: date, starts: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for start in starts:
        kb.button(
            text=start,
            callback_data=MoveTimeCB(
                booking=booking_id, day=day.isoformat(), start=minutes_of(parse_hhmm(start))
            ),
        )
    kb.adjust(4)
    kb.row(
        InlineKeyboardButton(
            text="‹ К датам", callback_data=MoveCB(booking=booking_id).pack()
        )
    )
    return kb.as_markup()


def move_confirm_kb(booking_id: str, day: date, start: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Перенести запись",
        callback_data=MoveOkCB(
            booking=booking_id, day=day.isoformat(), start=minutes_of(parse_hhmm(start))
        ),
    )
    kb.button(
        text="‹ Выбрать другое время",
        callback_data=MoveDayCB(booking=booking_id, day=day.isoformat()),
    )
    kb.adjust(1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="‹ В меню", callback_data=MenuCB(action="root"))
    return kb.as_markup()


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже",
    )


def drop_reply_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
