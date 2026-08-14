"""Расчёт свободного времени.

Здесь только арифметика по календарю: ни сети, ни хранилища — поэтому всё это
покрыто тестами и легко переносится под другой график работы.

Смена специалиста режется на клетки по `slot_step_minutes`. Услуга длиной 90 минут при
шаге 30 занимает три клетки подряд — значит начать её можно только там, где все три
свободны и укладываются в смену.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import BookingConfig, Service, Specialist

WEEKDAY_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTH_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def fmt_hhmm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def minutes_of(value: time) -> int:
    """Время → минуты от полуночи. В callback-данных двоеточие занято разделителем."""
    return value.hour * 60 + value.minute


def time_of_minutes(minutes: int) -> time:
    return time(minutes // 60, minutes % 60)


def fmt_date(day: date) -> str:
    return f"{day.day} {MONTH_RU[day.month - 1]}, {WEEKDAY_RU[day.weekday()]}"


def slot_cells(start: time, duration: int, step: int) -> list[time]:
    """Клетки расписания, которые занимает услуга, начатая в `start`."""
    base = datetime.combine(date(2000, 1, 1), start)
    count = -(-duration // step)  # округление вверх
    return [(base + timedelta(minutes=step * i)).time() for i in range(count)]


def shift_starts(
    specialist: Specialist, day: date, service: Service, config: BookingConfig
) -> list[time]:
    """Все начала услуги, которые помещаются в смену специалиста в этот день."""
    shift = specialist.works_on(day.weekday())
    if not shift or not specialist.does(service.id):
        return []

    opens, closes = parse_hhmm(shift[0]), parse_hhmm(shift[1])
    step = config.slot_step_minutes
    cursor = datetime.combine(day, opens)
    end_of_shift = datetime.combine(day, closes)

    starts: list[time] = []
    while cursor + timedelta(minutes=service.duration) <= end_of_shift:
        starts.append(cursor.time())
        cursor += timedelta(minutes=step)
    return starts


def is_bookable(day: date, start: time, config: BookingConfig, now: datetime) -> bool:
    """Отсекаем прошедшее время и слишком поздние записи «через пять минут»."""
    moment = datetime.combine(day, start, tzinfo=config.tz)
    return moment - timedelta(minutes=config.min_lead_minutes) >= now


def booking_days(config: BookingConfig, now: datetime) -> list[date]:
    today = now.astimezone(config.tz).date()
    return [today + timedelta(days=i) for i in range(config.booking_depth_days)]


def busy_key(specialist_id: str, day: date, cell: time) -> str:
    return f"busy:{specialist_id}:{day.isoformat()}:{fmt_hhmm(cell)}"
