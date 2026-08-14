from datetime import date

import pytest

from bot.booking import Booking
from bot.config import BookingConfig, Service, Specialist, get_config
from bot.texts import (
    admin_day,
    bot_description,
    bot_name,
    bot_short_description,
    booked,
    confirm_card,
    human_minutes,
    info,
    my_bookings,
    plural,
)

DAY = date(2026, 8, 3)


def make_config(**overrides) -> BookingConfig:
    """Минимальный конфиг: только услуга и специалист, ни адреса, ни цены."""
    defaults = dict(
        services=(Service(id="free", title="Встреча", duration=30),),
        specialists=(
            Specialist(
                id="one",
                name="Специалист 1",
                services=("free",),
                schedule={"mon": ("10:00", "18:00")},
            ),
        ),
    )
    return BookingConfig(**{**defaults, **overrides})


def make_booking(**overrides) -> Booking:
    defaults = dict(
        id="b1",
        user_id=1,
        user_name="Клиент",
        phone="+79000000000",
        username="",
        service_id="free",
        specialist_id="one",
        day=DAY.isoformat(),
        start="12:00",
        created_at="2026-08-01T10:00:00",
    )
    return Booking(**{**defaults, **overrides})


@pytest.mark.parametrize(
    "count,expected",
    [(1, "запись"), (2, "записи"), (4, "записи"), (5, "записей"),
     (11, "записей"), (12, "записей"), (21, "запись"), (22, "записи"), (25, "записей")],
)
def test_plural(count, expected):
    assert plural(count, "запись", "записи", "записей") == expected


@pytest.mark.parametrize(
    "minutes,expected", [(30, "30 минут"), (60, "час"), (90, "90 минут"), (120, "2 часа")]
)
def test_human_minutes(minutes, expected):
    assert human_minutes(minutes) == expected


def test_profile_texts_fit_telegram_limits():
    """Telegram отказывается принимать описание длиннее лимита — витрина останется пустой."""
    config = get_config()
    assert 0 < len(bot_name(config)) <= 64
    assert 0 < len(bot_short_description(config)) <= 120
    assert 0 < len(bot_description(config)) <= 512
    assert config.title in bot_description(config)


def test_texts_survive_config_without_contacts():
    """Адрес и телефон необязательны: пустых строк и «None» в сообщениях быть не должно."""
    config = make_config()
    booking = make_booking()
    for text in (bot_description(config), info(config), booked(config, booking)):
        assert "None" not in text
        assert "\n\n\n" not in text
        assert not text.endswith("\n")


def test_contacts_show_up_when_filled():
    config = make_config(address="г. Город, ул. Улица, 1", phone="+7 900 000-00-00")
    assert "г. Город, ул. Улица, 1" in info(config)
    assert "+7 900 000-00-00" in info(config)
    assert config.address in booked(config, make_booking())


def test_service_without_price_is_shown_without_price():
    """Цена необязательна: там, где её нет, не должно остаться ни «0 ₽», ни пустой строки."""
    config = make_config()
    draft = {"service": "free", "specialist": "one", "day": DAY.isoformat(), "start": "12:00"}
    for text in (confirm_card(config, draft), my_bookings(config, [make_booking()])):
        assert "₽" not in text
    assert "Стоимость" not in confirm_card(config, draft)
    assert "на 0 ₽" not in admin_day(config, DAY, [make_booking()])


def test_price_is_shown_when_set():
    config = make_config(services=(Service(id="free", title="Встреча", duration=30, price=1500),))
    draft = {"service": "free", "specialist": "one", "day": DAY.isoformat(), "start": "12:00"}
    assert "1 500 ₽" in confirm_card(config, draft)
    assert "1 500 ₽" in admin_day(config, DAY, [make_booking()])
