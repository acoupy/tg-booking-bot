"""Диалог бота.

Клиентский путь: меню → услуга → специалист → день → время → подтверждение → телефон.
Админский: уведомления о новых записях и отменах + расписание на день командой.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, Message

from . import texts
from .booking import (
    cancel_booking,
    create_booking,
    day_bookings,
    free_starts,
    free_starts_by_specialist,
    get_booking,
    get_client,
    move_booking,
    pop_pending,
    save_client,
    save_pending,
    user_bookings,
)
from .config import BookingConfig, admin_ids
from .keyboards import (
    ANY_SPECIALIST,
    CancelCB,
    ConfirmCB,
    DayCB,
    MenuCB,
    MoveCB,
    MoveDayCB,
    MoveOkCB,
    MoveTimeCB,
    ServiceCB,
    SpecialistCB,
    TimeCB,
    back_kb,
    confirm_kb,
    days_kb,
    drop_reply_kb,
    main_menu,
    move_confirm_kb,
    move_days_kb,
    move_times_kb,
    my_bookings_kb,
    phone_kb,
    services_kb,
    specialists_kb,
    times_kb,
)
from .schedule import booking_days, fmt_hhmm, parse_hhmm, shift_starts, time_of_minutes
from .storage import Store

log = logging.getLogger(__name__)
router = Router()


def _now(config: BookingConfig) -> datetime:
    return datetime.now(config.tz)


async def _edit(callback: CallbackQuery, text: str, markup=None) -> None:
    """Правит текущее сообщение; если Telegram считает его неизменным — молчим."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


async def _notify_admins(bot: Bot, text: str) -> None:
    for admin_id in admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # админ мог не начать диалог с ботом
            log.warning("не удалось уведомить админа %s", admin_id, exc_info=True)


async def _at_limit(store: Store, config: BookingConfig, user_id: int) -> bool:
    """Сколько записей клиент держит одновременно — ограничение из конфига.

    Бот публичный: без потолка один человек может занять всё расписание вперёд,
    и владелец увидит это уже по пустому дню.
    """
    active = await user_bookings(store, config, user_id, _now(config))
    return len(active) >= config.max_active_bookings


def _specialists_for(config: BookingConfig, service_id: str, chosen: str):
    if chosen == ANY_SPECIALIST:
        return config.specialists_for(service_id)
    return tuple(filter(None, [config.specialist(chosen)]))


# --- меню -------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, config: BookingConfig) -> None:
    await message.answer(
        texts.greeting(config, message.from_user.first_name),
        reply_markup=main_menu(message.from_user.id in admin_ids()),
    )


@router.callback_query(MenuCB.filter(F.action == "root"))
async def show_menu(callback: CallbackQuery, config: BookingConfig) -> None:
    await _edit(
        callback,
        texts.greeting(config, callback.from_user.first_name),
        main_menu(callback.from_user.id in admin_ids()),
    )
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "info"))
async def show_info(callback: CallbackQuery, config: BookingConfig) -> None:
    await _edit(callback, texts.info(config), back_kb())
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "book"))
async def show_services(callback: CallbackQuery, config: BookingConfig, store: Store) -> None:
    if await _at_limit(store, config, callback.from_user.id):
        await _edit(callback, texts.limit_reached(config), back_kb())
        await callback.answer()
        return
    await _edit(callback, texts.choose_service(), services_kb(config))
    await callback.answer()


# --- выбор услуги, специалиста, дня, времени --------------------------------


@router.callback_query(ServiceCB.filter())
async def show_specialists(
    callback: CallbackQuery, callback_data: ServiceCB, config: BookingConfig
) -> None:
    service = config.service(callback_data.service)
    if service is None:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return
    await _edit(callback, texts.choose_specialist(service.title), specialists_kb(config, service))
    await callback.answer()


@router.callback_query(SpecialistCB.filter())
async def show_days(
    callback: CallbackQuery, callback_data: SpecialistCB, config: BookingConfig
) -> None:
    service = config.service(callback_data.service)
    if service is None:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return

    specialists = _specialists_for(config, service.id, callback_data.specialist)
    # Дни, в которые хоть кто-то из выбранных специалистов вообще выходит на смену.
    # Занятость здесь не проверяем: это десятки запросов в хранилище ради экрана,
    # который всё равно ведёт на список времени.
    days = [
        day
        for day in booking_days(config, _now(config))
        if any(shift_starts(specialist, day, service, config) for specialist in specialists)
    ]
    label = (
        "любой специалист"
        if callback_data.specialist == ANY_SPECIALIST
        else specialists[0].name if specialists else "—"
    )
    await _edit(
        callback,
        texts.choose_day(service.title, label),
        days_kb(service.id, callback_data.specialist, days),
    )
    await callback.answer()


@router.callback_query(DayCB.filter())
async def show_times(
    callback: CallbackQuery, callback_data: DayCB, config: BookingConfig, store: Store
) -> None:
    service = config.service(callback_data.service)
    if service is None:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return

    day = date.fromisoformat(callback_data.day)
    specialists = _specialists_for(config, service.id, callback_data.specialist)
    slots = await free_starts_by_specialist(store, config, specialists, day, service, _now(config))
    if not slots:
        await _edit(
            callback,
            texts.no_time(day),
            days_kb(
                service.id,
                callback_data.specialist,
                [
                    d
                    for d in booking_days(config, _now(config))
                    if any(shift_starts(s, d, service, config) for s in specialists)
                ],
            ),
        )
        await callback.answer()
        return

    label = (
        "любой специалист"
        if callback_data.specialist == ANY_SPECIALIST
        else specialists[0].name
    )
    await _edit(
        callback,
        texts.choose_time(service.title, label, day),
        times_kb(service.id, callback_data.specialist, day, list(slots)),
    )
    await callback.answer()


@router.callback_query(TimeCB.filter())
async def show_confirm(
    callback: CallbackQuery, callback_data: TimeCB, config: BookingConfig, store: Store
) -> None:
    service = config.service(callback_data.service)
    if service is None:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return

    day = date.fromisoformat(callback_data.day)
    specialists = _specialists_for(config, service.id, callback_data.specialist)
    start = fmt_hhmm(time_of_minutes(callback_data.start))
    slots = await free_starts_by_specialist(store, config, specialists, day, service, _now(config))
    available = slots.get(start)
    if not available:
        await callback.answer(texts.slot_taken(), show_alert=True)
        await show_times(
            callback,
            DayCB(service=service.id, specialist=callback_data.specialist, day=callback_data.day),
            config,
            store,
        )
        return

    # «Любой специалист» превращается в конкретного человека уже здесь, чтобы клиент
    # видел в подтверждении имя, а не «кого-нибудь».
    draft = {
        "service": service.id,
        "specialist": available[0],
        "day": callback_data.day,
        "start": start,
    }
    await save_pending(store, callback.from_user.id, draft)
    await _edit(
        callback,
        texts.confirm_card(config, draft),
        confirm_kb(service.id, callback_data.specialist, day),
    )
    await callback.answer()


# --- подтверждение и бронь --------------------------------------------------


async def _finish_booking(
    *,
    bot: Bot,
    chat_id: int,
    user,
    draft: dict,
    phone: str,
    config: BookingConfig,
    store: Store,
) -> None:
    service = config.service(draft["service"])
    specialist = config.specialist(draft["specialist"])
    day = date.fromisoformat(draft["day"])
    booking = await create_booking(
        store,
        config,
        user_id=user.id,
        user_name=user.full_name,
        phone=phone,
        username=user.username or "",
        service=service,
        specialist=specialist,
        day=day,
        start=parse_hhmm(draft["start"]),
        now=_now(config),
    )
    if booking is None:
        await bot.send_message(chat_id, texts.slot_taken(), reply_markup=back_kb())
        return

    await bot.send_message(chat_id, texts.booked(config, booking), reply_markup=drop_reply_kb())
    await bot.send_message(chat_id, "Что-нибудь ещё?", reply_markup=main_menu(user.id in admin_ids()))
    await _notify_admins(bot, texts.admin_new_booking(config, booking))


@router.callback_query(ConfirmCB.filter())
async def confirm(callback: CallbackQuery, config: BookingConfig, store: Store, bot: Bot) -> None:
    draft = await pop_pending(store, callback.from_user.id)
    if draft is None:
        await callback.answer("Выбор устарел, начнём заново", show_alert=True)
        await show_services(callback, config, store)
        return

    if await _at_limit(store, config, callback.from_user.id):
        await callback.answer()
        await _edit(callback, texts.limit_reached(config), back_kb())
        return

    client = await get_client(store, callback.from_user.id)
    if client is None:
        # Телефон ещё не знаем: возвращаем черновик в хранилище и просим контакт.
        await save_pending(store, callback.from_user.id, draft)
        await _edit(callback, texts.confirm_card(config, draft))
        await callback.message.answer(texts.ask_phone(), reply_markup=phone_kb())
        await callback.answer()
        return

    await callback.answer("Записываю…")
    await _edit(callback, texts.confirm_card(config, draft))
    await _finish_booking(
        bot=bot,
        chat_id=callback.message.chat.id,
        user=callback.from_user,
        draft=draft,
        phone=client["phone"],
        config=config,
        store=store,
    )


@router.message(F.contact)
async def got_contact(message: Message, config: BookingConfig, store: Store, bot: Bot) -> None:
    if message.contact.user_id != message.from_user.id:
        await message.answer(
            "Нужен ваш номер — нажмите кнопку «Отправить номер телефона».",
            reply_markup=phone_kb(),
        )
        return

    await save_client(store, message.from_user.id, message.from_user.full_name, message.contact.phone_number)
    draft = await pop_pending(store, message.from_user.id)
    if draft is None:
        await message.answer(
            "Спасибо! Номер сохранил. Выберите время — запись займёт минуту.",
            reply_markup=drop_reply_kb(),
        )
        await message.answer(texts.choose_service(), reply_markup=services_kb(config))
        return

    await _finish_booking(
        bot=bot,
        chat_id=message.chat.id,
        user=message.from_user,
        draft=draft,
        phone=message.contact.phone_number,
        config=config,
        store=store,
    )


# --- мои записи -------------------------------------------------------------


@router.callback_query(MenuCB.filter(F.action == "my"))
async def show_my(callback: CallbackQuery, config: BookingConfig, store: Store) -> None:
    bookings = await user_bookings(store, config, callback.from_user.id, _now(config))
    await _edit(
        callback,
        texts.my_bookings(config, bookings),
        my_bookings_kb([b.id for b in bookings]) if bookings else back_kb(),
    )
    await callback.answer()


@router.callback_query(CancelCB.filter())
async def cancel(
    callback: CallbackQuery,
    callback_data: CancelCB,
    config: BookingConfig,
    store: Store,
    bot: Bot,
) -> None:
    booking = await cancel_booking(store, config, callback_data.booking)
    if booking is None:
        await callback.answer("Запись уже отменена", show_alert=True)
    else:
        await callback.answer("Отменил")
        await _notify_admins(bot, texts.admin_cancelled(config, booking))
    bookings = await user_bookings(store, config, callback.from_user.id, _now(config))
    await _edit(
        callback,
        (texts.cancelled(config, booking) + "\n\n" if booking else "")
        + texts.my_bookings(config, bookings),
        my_bookings_kb([b.id for b in bookings]) if bookings else back_kb(),
    )


# --- перенос записи ---------------------------------------------------------
#
# Услуга и специалист при переносе не меняются — меняется только время, поэтому в
# кнопках едет один номер записи, а остальное берётся из неё самой.


async def _load_for_move(callback: CallbackQuery, config: BookingConfig, store: Store, booking_id: str):
    """Запись, её услуга и специалист. None — записи уже нет, показываем список заново."""
    booking = await get_booking(store, booking_id)
    service = config.service(booking.service_id) if booking else None
    specialist = config.specialist(booking.specialist_id) if booking else None
    if booking is None or service is None or specialist is None:
        await callback.answer("Этой записи уже нет", show_alert=True)
        await show_my(callback, config, store)
        return None
    return booking, service, specialist


@router.callback_query(MoveCB.filter())
async def move_pick_day(
    callback: CallbackQuery, callback_data: MoveCB, config: BookingConfig, store: Store
) -> None:
    loaded = await _load_for_move(callback, config, store, callback_data.booking)
    if loaded is None:
        return
    booking, service, specialist = loaded
    days = [
        day
        for day in booking_days(config, _now(config))
        if shift_starts(specialist, day, service, config)
    ]
    await _edit(callback, texts.move_choose_day(config, booking), move_days_kb(booking.id, days))
    await callback.answer()


@router.callback_query(MoveDayCB.filter())
async def move_pick_time(
    callback: CallbackQuery, callback_data: MoveDayCB, config: BookingConfig, store: Store
) -> None:
    loaded = await _load_for_move(callback, config, store, callback_data.booking)
    if loaded is None:
        return
    booking, service, specialist = loaded
    day = date.fromisoformat(callback_data.day)
    # Собственные клетки записи для неё самой свободны — иначе она бы блокировала
    # и своё время, и соседние окна рядом с ним.
    starts = await free_starts(
        store, config, specialist, day, service, _now(config), ignore=booking
    )
    if not starts:
        days = [
            d
            for d in booking_days(config, _now(config))
            if shift_starts(specialist, d, service, config)
        ]
        await _edit(callback, texts.no_time(day), move_days_kb(booking.id, days))
        await callback.answer()
        return

    await _edit(
        callback,
        texts.move_choose_time(config, booking, day),
        move_times_kb(booking.id, day, [fmt_hhmm(start) for start in starts]),
    )
    await callback.answer()


@router.callback_query(MoveTimeCB.filter())
async def move_confirm_screen(
    callback: CallbackQuery, callback_data: MoveTimeCB, config: BookingConfig, store: Store
) -> None:
    loaded = await _load_for_move(callback, config, store, callback_data.booking)
    if loaded is None:
        return
    booking, _service, _specialist = loaded
    day = date.fromisoformat(callback_data.day)
    start = fmt_hhmm(time_of_minutes(callback_data.start))
    await _edit(
        callback,
        texts.move_confirm_card(config, booking, day, start),
        move_confirm_kb(booking.id, day, start),
    )
    await callback.answer()


@router.callback_query(MoveOkCB.filter())
async def move_apply(
    callback: CallbackQuery, callback_data: MoveOkCB, config: BookingConfig, store: Store, bot: Bot
) -> None:
    loaded = await _load_for_move(callback, config, store, callback_data.booking)
    if loaded is None:
        return
    before, service, specialist = loaded
    day = date.fromisoformat(callback_data.day)
    start = time_of_minutes(callback_data.start)

    # Кнопка могла пролежать в чате долго: проверяем время заново, как при записи.
    free = await free_starts(store, config, specialist, day, service, _now(config), ignore=before)
    after = (
        await move_booking(store, config, before.id, day=day, start=start, now=_now(config))
        if start in free
        else None
    )
    if after is None:
        await callback.answer(texts.slot_taken(), show_alert=True)
        await move_pick_time(
            callback, MoveDayCB(booking=before.id, day=callback_data.day), config, store
        )
        return

    await callback.answer("Перенёс")
    bookings = await user_bookings(store, config, callback.from_user.id, _now(config))
    await _edit(
        callback,
        texts.moved(config, before, after) + "\n\n" + texts.my_bookings(config, bookings),
        my_bookings_kb([b.id for b in bookings]) if bookings else back_kb(),
    )
    await _notify_admins(bot, texts.admin_moved(config, before, after))


# --- админ ------------------------------------------------------------------


@router.message(Command("today"))
async def cmd_today(message: Message, config: BookingConfig, store: Store) -> None:
    if message.from_user.id not in admin_ids():
        return
    day = _now(config).date()
    await message.answer(texts.admin_day(config, day, await day_bookings(store, day)))


@router.message(Command("week"))
async def cmd_week(message: Message, config: BookingConfig, store: Store) -> None:
    if message.from_user.id not in admin_ids():
        return
    today = _now(config).date()
    days = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        bookings = await day_bookings(store, day)
        if bookings:
            days.append((day, bookings))
            await message.answer(texts.admin_day(config, day, bookings))
    await message.answer(texts.admin_week_total(config, days))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.callback_query(F.data == "admin:today")
async def admin_today(callback: CallbackQuery, config: BookingConfig, store: Store) -> None:
    if callback.from_user.id not in admin_ids():
        await callback.answer()
        return
    day = _now(config).date()
    await _edit(callback, texts.admin_day(config, day, await day_bookings(store, day)), back_kb())
    await callback.answer()


# --- всё остальное ----------------------------------------------------------


@router.message()
async def fallback(message: Message, config: BookingConfig) -> None:
    await message.answer(
        "Записаться и посмотреть свои записи можно кнопками ниже 👇",
        reply_markup=main_menu(message.from_user.id in admin_ids()),
    )


# --- ошибки -----------------------------------------------------------------


async def _quiet(coro) -> None:
    """Ответ на ошибку сам может не пройти (устаревшее нажатие, заблокированный бот)."""
    try:
        await coro
    except Exception:
        log.warning("не удалось ответить на ошибку", exc_info=True)


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Последний рубеж: без него упавший хендлер оставляет человека без ответа.

    Telegram в любом случае получает 200 (иначе апдейт приедет по кругу), поэтому
    сбой выглядит как зависший бот: нажал кнопку — тишина, жмёт ещё раз. Здесь клиент
    получает внятный ответ, а владелец — что именно сломалось и на каком шаге.
    Возврат True говорит aiogram, что исключение обработано и дальше его нести не надо.
    """
    message, callback = event.update.message, event.update.callback_query
    error = event.exception
    log.exception(
        "апдейт %s упал: %s", event.update.update_id, type(error).__name__, exc_info=error
    )

    if callback is not None:
        # Всплывающее окно привязано к самому нажатию — в чат ничего не досыпаем.
        await _quiet(callback.answer(texts.error_alert(), show_alert=True))
    elif message is not None:
        await _quiet(message.answer(texts.error_alert()))

    sender = (message or callback).from_user if (message or callback) else None
    await _notify_admins(
        bot,
        texts.admin_error(
            step=(callback.data if callback else (message.text if message else "")) or "",
            who=f"{sender.full_name} (id {sender.id})" if sender else "",
            error=f"{type(error).__name__}: {error}",
            update_id=event.update.update_id,
        ),
    )
    return True
