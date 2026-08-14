"""Тексты сообщений. Вынесены отдельно, чтобы правки под клиента не лезли в логику.

Название и контакты в конфиге необязательны, поэтому здесь ни одна строка не
рассчитывает, что адрес или телефон заполнены: чего нет, то просто не печатается.
"""

from __future__ import annotations

from datetime import date
from html import escape

from .booking import Booking
from .config import BookingConfig
from .schedule import fmt_date


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 запись», «2 записи», «5 записей» — обычные русские правила."""
    tail_two, tail_one = count % 100, count % 10
    if 11 <= tail_two <= 14:
        return many
    if tail_one == 1:
        return one
    if 2 <= tail_one <= 4:
        return few
    return many


def human_minutes(minutes: int) -> str:
    """60 → «час», 90 → «90 минут»: срок в правилах читается словами, а не числом в минутах."""
    if minutes % 60 or minutes < 60:
        return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours = minutes // 60
    return "час" if hours == 1 else f"{hours} {plural(hours, 'час', 'часа', 'часов')}"


def contact_lines(config: BookingConfig) -> str:
    return "\n".join(f"{icon} {value}" for icon, value in config.contacts)


def bot_name(config: BookingConfig) -> str:
    """Имя бота в Telegram. Лимит — 64 символа."""
    return config.title[:64]


def bot_short_description(config: BookingConfig) -> str:
    """Строчка под именем бота в поиске и в пустом чате. Telegram даёт 120 символов."""
    return f"{config.title}: услуга, специалист и время — за минуту, без звонков."[:120]


def bot_description(config: BookingConfig) -> str:
    """Экран «Что умеет этот бот» до нажатия «Начать». Telegram даёт 512 символов."""
    text = (
        f"{config.title}.\n\n"
        "Выберите услугу, специалиста и удобное время — свободные окна бот считает сам, "
        "по расписанию смен. Утром в день визита придёт напоминание, "
        "а перенести или отменить запись можно в пару нажатий."
    )
    contacts = contact_lines(config)
    return (f"{text}\n\n{contacts}" if contacts else text)[:512]


def greeting(config: BookingConfig, name: str) -> str:
    return (
        f"<b>{config.title}</b>\n\n"
        f"{name}, здравствуйте! Здесь можно записаться за минуту — без звонков.\n"
        f"Запись открыта на {config.booking_depth_days} дней вперёд."
    )


def info(config: BookingConfig) -> str:
    """Экран «Как это работает»: правила записи, а под ними контакты, если они заданы."""
    lines = [f"<b>{config.title}</b>", ""]
    if config.about:
        lines += [config.about, ""]
    lines += [
        f"• Записаться можно на {config.booking_depth_days} дней вперёд.",
        f"• Ближайшее время — не позже чем за {human_minutes(config.min_lead_minutes)} до начала.",
        "• Свободное время считается по сменам специалистов и длительности услуги.",
        f"• Одновременно можно держать до {config.max_active_bookings} "
        f"{plural(config.max_active_bookings, 'записи', 'записей', 'записей')}.",
        "• Перенести или отменить запись — в «Мои записи», в любой момент.",
        "• Утром в день визита придёт напоминание.",
    ]
    contacts = contact_lines(config)
    if contacts:
        lines += ["", contacts]
    return "\n".join(lines)


def choose_service() -> str:
    return "Выберите услугу:"


def choose_specialist(service_title: str) -> str:
    return f"<b>{service_title}</b>\nК кому записываемся?"


def choose_day(service_title: str, specialist_label: str) -> str:
    return f"<b>{service_title}</b> · {specialist_label}\nВыберите дату:"


def choose_time(service_title: str, specialist_label: str, day: date) -> str:
    return f"<b>{service_title}</b> · {specialist_label}\n{fmt_date(day)} — свободное время:"


def no_time(day: date) -> str:
    return (
        f"На {fmt_date(day)} свободного времени не осталось.\n"
        f"Выберите другую дату или другого специалиста."
    )


def confirm_card(config: BookingConfig, booking_draft: dict) -> str:
    service = config.service(booking_draft["service"])
    specialist = config.specialist(booking_draft["specialist"])
    day = date.fromisoformat(booking_draft["day"])
    # Длительность в карточке не показываем: она нужна расписанию, чтобы не сажать
    # следующего клиента внахлёст, но клиенту это выглядело бы как обещание уложиться
    # ровно в час — а специалист такого не обещал.
    lines = [
        "<b>Проверьте запись</b>",
        "",
        f"Услуга: {service.title}",
        f"Специалист: {specialist.name}",
        f"Когда: {fmt_date(day)}, {booking_draft['start']}",
    ]
    if service.price_label:
        lines.append(f"Стоимость: {service.price_label}")
    return "\n".join(lines)


def ask_phone() -> str:
    return (
        "Остался один шаг — телефон, чтобы с вами могли связаться, если что-то изменится.\n"
        "Нажмите кнопку ниже, номер подставится автоматически."
    )


def limit_reached(config: BookingConfig) -> str:
    word = plural(config.max_active_bookings, "запись", "записи", "записей")
    return (
        f"У вас уже {config.max_active_bookings} {word} — это максимум.\n"
        f"Отмените ненужную в «Мои записи», и можно будет выбрать новое время."
    )


def booked(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    specialist = config.specialist(booking.specialist_id)
    lines = [
        "✅ <b>Записал вас</b>",
        "",
        f"{service.title} · {specialist.name}",
        f"{fmt_date(booking.date)}, {booking.start}",
    ]
    if config.address:
        lines.append(config.address)
    lines += ["", "Напомню утром в день визита. Отменить запись можно в «Мои записи»."]
    return "\n".join(lines)


def slot_taken() -> str:
    return "Это время только что заняли 😔 Выберите, пожалуйста, другое."


def my_bookings(config: BookingConfig, bookings: list[Booking]) -> str:
    if not bookings:
        return "У вас пока нет активных записей."
    lines = ["<b>Ваши записи</b>", ""]
    for index, booking in enumerate(bookings, start=1):
        service = config.service(booking.service_id)
        specialist = config.specialist(booking.specialist_id)
        price = f" · {service.price_label}" if service.price_label else ""
        lines.append(
            f"{index}. {fmt_date(booking.date)}, {booking.start} — "
            f"{service.title} · {specialist.name}{price}"
        )
    return "\n".join(lines)


def _booking_line(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    specialist = config.specialist(booking.specialist_id)
    return f"{service.title} · {specialist.name}"


def move_choose_day(config: BookingConfig, booking: Booking) -> str:
    return (
        f"<b>Перенос записи</b>\n{_booking_line(config, booking)}\n"
        f"Сейчас: {fmt_date(booking.date)}, {booking.start}\n\n"
        f"Выберите новую дату:"
    )


def move_choose_time(config: BookingConfig, booking: Booking, day: date) -> str:
    return (
        f"<b>Перенос записи</b>\n{_booking_line(config, booking)}\n"
        f"{fmt_date(day)} — свободное время:"
    )


def move_confirm_card(config: BookingConfig, booking: Booking, day: date, start: str) -> str:
    return (
        f"<b>Перенести запись?</b>\n\n"
        f"{_booking_line(config, booking)}\n"
        f"Было: {fmt_date(booking.date)}, {booking.start}\n"
        f"Станет: {fmt_date(day)}, {start}"
    )


def moved(config: BookingConfig, before: Booking, after: Booking) -> str:
    return (
        f"🔁 <b>Перенёс запись</b>\n\n"
        f"{_booking_line(config, after)}\n"
        f"Было: {fmt_date(before.date)}, {before.start}\n"
        f"Стало: {fmt_date(after.date)}, {after.start}"
    )


def admin_moved(config: BookingConfig, before: Booking, after: Booking) -> str:
    return (
        f"🔁 <b>Перенос</b>\n\n"
        f"{_booking_line(config, after)}\n"
        f"Было: {fmt_date(before.date)}, {before.start}\n"
        f"Стало: {fmt_date(after.date)}, {after.start}\n"
        f"Клиент: {after.user_name} · {after.phone}"
    )


def cancelled(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    return f"Запись отменена: {service.title}, {fmt_date(booking.date)} в {booking.start}."


def admin_new_booking(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    specialist = config.specialist(booking.specialist_id)
    contact = f"@{booking.username}" if booking.username else "—"
    price = f" · {service.price_label}" if service.price_label else ""
    return (
        "🆕 <b>Новая запись</b>\n\n"
        f"{fmt_date(booking.date)}, {booking.start}\n"
        f"{service.title} · {specialist.name}{price}\n"
        f"Клиент: {booking.user_name} ({contact})\n"
        f"Телефон: {booking.phone}"
    )


def admin_cancelled(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    specialist = config.specialist(booking.specialist_id)
    return (
        "🚫 <b>Отмена</b>\n\n"
        f"{fmt_date(booking.date)}, {booking.start}\n"
        f"{service.title} · {specialist.name}\n"
        f"Клиент: {booking.user_name} · {booking.phone}"
    )


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def admin_day(config: BookingConfig, day: date, bookings: list[Booking]) -> str:
    if not bookings:
        return f"<b>{fmt_date(day)}</b>\n\nЗаписей нет."
    lines = [f"<b>{fmt_date(day)}</b>", ""]
    revenue = 0
    for booking in bookings:
        service = config.service(booking.service_id)
        specialist = config.specialist(booking.specialist_id)
        revenue += service.price if service else 0
        lines.append(
            f"{booking.start} — {service.title} · {specialist.name}\n"
            f"    {booking.user_name}, {booking.phone}"
        )
    lines.append("")
    word = plural(len(bookings), "запись", "записи", "записей")
    total = f"Итого: {len(bookings)} {word}"
    lines.append(f"{total} на {money(revenue)}" if revenue else total)
    return "\n".join(lines)


def admin_week_total(config: BookingConfig, days: list[tuple[date, list[Booking]]]) -> str:
    """Итог недели. Пустую неделю проговариваем прямо — «это всё» ни о чём не говорит."""
    if not days:
        return (
            "На ближайшие 7 дней записей нет.\n"
            "Как только кто-то запишется, я пришлю уведомление сюда."
        )
    count = sum(len(bookings) for _day, bookings in days)
    revenue = sum(
        (config.service(booking.service_id).price if config.service(booking.service_id) else 0)
        for _day, bookings in days
        for booking in bookings
    )
    total = f"<b>Итого за 7 дней:</b> {count} {plural(count, 'запись', 'записи', 'записей')}"
    return (
        (f"{total} на {money(revenue)}" if revenue else total)
        + f"\nЗанятых дней: {len(days)} из 7."
    )


def error_alert() -> str:
    """Показывается всплывающим окном, поэтому без разметки и в пределах 200 символов."""
    return (
        "Что-то пошло не так с моей стороны. Попробуйте ещё раз "
        "или наберите /start — записи при этом не теряются."
    )


def admin_error(step: str, who: str, error: str, update_id: int | None) -> str:
    """Владельцу — что сломалось и на каком шаге; текст ошибки экранируем, в нём бывает «<»."""
    return (
        "⚠️ <b>Ошибка в боте</b>\n\n"
        f"Шаг: {escape(step or '—')}\n"
        f"Клиент: {escape(who or '—')}\n"
        f"<code>{escape(error)}</code>\n\n"
        f"Апдейт {update_id if update_id is not None else '—'} — по этому номеру "
        f"ищется полная запись в логах хостинга."
    )


def reminder(config: BookingConfig, booking: Booking) -> str:
    service = config.service(booking.service_id)
    specialist = config.specialist(booking.specialist_id)
    text = (
        f"⏰ Напоминаю: сегодня в <b>{booking.start}</b> вы записаны — "
        f"{service.title} · {specialist.name}."
    )
    return f"{text}\n{config.address}" if config.address else text
