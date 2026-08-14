from datetime import date, datetime, time

import pytest

from bot.config import get_config
from bot.schedule import fmt_hhmm, is_bookable, shift_starts, slot_cells

MONDAY = date(2026, 8, 3)
TUESDAY = date(2026, 8, 4)
SUNDAY = date(2026, 8, 9)


@pytest.fixture
def config():
    return get_config()


def test_slot_cells_covers_whole_service():
    assert slot_cells(time(12, 0), duration=90, step=30) == [time(12, 0), time(12, 30), time(13, 0)]
    assert slot_cells(time(12, 0), duration=30, step=30) == [time(12, 0)]


def test_service_must_fit_into_shift(config):
    """Первый специалист работает в понедельник 10:00–20:00, услуга длится 90 минут."""
    long_service = config.service("s90")
    starts = shift_starts(config.specialist("spec1"), MONDAY, long_service, config)
    assert fmt_hhmm(starts[0]) == "10:00"
    assert fmt_hhmm(starts[-1]) == "18:30"  # 18:30 + 90 мин = ровно конец смены


def test_day_off_gives_no_slots(config):
    assert shift_starts(config.specialist("spec1"), SUNDAY, config.service("s60"), config) == []


def test_specialist_without_service_gives_no_slots(config):
    """Второй специалист не делает услугу на 90 минут — даже в свою смену (вторник)."""
    assert shift_starts(config.specialist("spec2"), TUESDAY, config.service("s60"), config)
    assert shift_starts(config.specialist("spec2"), TUESDAY, config.service("s90"), config) == []


def test_past_and_too_soon_are_not_bookable(config):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=config.tz)
    assert not is_bookable(MONDAY, time(11, 0), config, now)  # уже прошло
    assert not is_bookable(MONDAY, time(12, 30), config, now)  # меньше часа на сборы
    assert is_bookable(MONDAY, time(13, 0), config, now)
