import json
from datetime import date, datetime, time, timedelta

import pytest

from bot.booking import (
    HISTORY_DAYS,
    busyday_key,
    cancel_booking,
    create_booking,
    day_bookings,
    free_starts,
    free_starts_by_specialist,
    get_booking,
    move_booking,
    purge_expired,
    user_bookings,
)
from bot.config import get_config
from bot.schedule import busy_key, fmt_hhmm
from bot.storage import JsonStore

MONDAY = date(2026, 8, 3)


@pytest.fixture
def config():
    return get_config()


@pytest.fixture
def store(tmp_path):
    return JsonStore(tmp_path / "bookings.json")


@pytest.fixture
def now(config):
    return datetime(2026, 8, 3, 9, 0, tzinfo=config.tz)


async def book(store, config, now, *, service="s90", specialist="spec1", start=time(12, 0), user=1):
    return await create_booking(
        store,
        config,
        user_id=user,
        user_name="Тест",
        phone="+79000000000",
        username="test",
        service=config.service(service),
        specialist=config.specialist(specialist),
        day=MONDAY,
        start=start,
        now=now,
    )


async def test_booking_blocks_its_own_time(store, config, now):
    assert await book(store, config, now) is not None
    free = await free_starts(
        store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
    )
    assert "12:00" not in [fmt_hhmm(t) for t in free]


async def test_long_service_blocks_overlapping_starts(store, config, now):
    """Услуга 12:00–13:30 закрывает и 11:00 (наехало бы), и 13:00 (наехало бы)."""
    await book(store, config, now)
    free = {
        fmt_hhmm(t)
        for t in await free_starts(
            store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
        )
    }
    assert "11:00" not in free  # 11:00 + 90 мин задело бы занятое
    assert "13:00" not in free
    assert "10:00" in free
    assert "13:30" in free


async def test_second_booking_on_same_slot_is_rejected(store, config, now):
    assert await book(store, config, now, user=1) is not None
    assert await book(store, config, now, user=2) is None


async def test_other_specialist_stays_free(store, config, now):
    """Занятость первого специалиста не должна закрывать время у третьего."""
    await book(store, config, now, specialist="spec1")
    slots = await free_starts_by_specialist(
        store, config, config.specialists_for("s90"), MONDAY, config.service("s90"), now
    )
    assert "spec1" not in slots["12:00"]
    assert "spec3" in slots["12:00"]


async def test_cancel_releases_time(store, config, now):
    booking = await book(store, config, now)
    assert await cancel_booking(store, config, booking.id) is not None
    free = await free_starts(
        store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
    )
    assert "12:00" in [fmt_hhmm(t) for t in free]
    assert await user_bookings(store, config, 1, now) == []
    assert await day_bookings(store, MONDAY) == []


async def test_indexes_see_the_booking(store, config, now):
    booking = await book(store, config, now, user=42)
    assert [b.id for b in await user_bookings(store, config, 42, now)] == [booking.id]
    assert [b.id for b in await day_bookings(store, MONDAY)] == [booking.id]


async def test_past_bookings_are_hidden_from_user(store, config, now):
    await book(store, config, now, start=time(12, 0))
    later = datetime(2026, 8, 3, 20, 0, tzinfo=config.tz)
    assert await user_bookings(store, config, 1, later) == []
    assert len(await user_bookings(store, config, 1, later, upcoming_only=False)) == 1


async def test_booking_of_older_format_is_treated_as_gone(store, config, now):
    """Записи из прошлой версии бота не должны ронять чтение — их просто нет."""
    booking = await book(store, config, now)
    raw = json.loads(await store.get(f"booking:{booking.id}"))
    raw["master_id"] = raw.pop("specialist_id")  # как это лежало до переименования
    await store.set(f"booking:{booking.id}", json.dumps(raw, ensure_ascii=False))

    assert await get_booking(store, booking.id) is None
    assert await day_bookings(store, MONDAY) == []
    assert await store.smembers(f"day:{MONDAY.isoformat()}") == []  # индекс подчистился


async def test_move_frees_the_old_time(store, config, now):
    booking = await book(store, config, now)
    moved = await move_booking(store, config, booking.id, day=MONDAY, start=time(15, 0), now=now)

    assert moved is not None and moved.start == "15:00"
    free = [
        fmt_hhmm(t)
        for t in await free_starts(
            store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
        )
    ]
    assert "12:00" in free  # старое время вернулось в продажу
    assert "15:00" not in free
    assert [b.start for b in await day_bookings(store, MONDAY)] == ["15:00"]


async def test_move_can_overlap_with_itself(store, config, now):
    """Сдвиг на полчаса: новая расстановка накрывает старую — свой же замок не мешает."""
    booking = await book(store, config, now)  # 12:00–13:30
    moved = await move_booking(store, config, booking.id, day=MONDAY, start=time(12, 30), now=now)

    assert moved is not None and moved.start == "12:30"
    assert await store.get(busy_key("spec1", MONDAY, time(12, 0))) is None
    assert await store.get(busy_key("spec1", MONDAY, time(13, 30))) is not None
    assert sorted(await store.smembers(busyday_key("spec1", MONDAY))) == ["12:30", "13:00", "13:30"]


async def test_move_onto_taken_time_changes_nothing(store, config, now):
    mine = await book(store, config, now, start=time(12, 0), user=1)
    await book(store, config, now, start=time(15, 0), user=2)

    assert await move_booking(store, config, mine.id, day=MONDAY, start=time(15, 0), now=now) is None
    assert (await get_booking(store, mine.id)).start == "12:00"
    assert await store.get(busy_key("spec1", MONDAY, time(12, 0))) is not None


async def test_move_to_another_day_moves_the_indexes(store, config, now):
    booking = await book(store, config, now)
    tuesday = MONDAY + timedelta(days=1)
    moved = await move_booking(store, config, booking.id, day=tuesday, start=time(12, 0), now=now)

    assert moved is not None
    assert await day_bookings(store, MONDAY) == []
    assert [b.id for b in await day_bookings(store, tuesday)] == [booking.id]
    assert [b.day for b in await user_bookings(store, config, 1, now)] == [tuesday.isoformat()]
    assert await store.smembers(busyday_key("spec1", MONDAY)) == []


async def test_moved_booking_does_not_block_itself(store, config, now):
    """При переносе своё же время должно показываться свободным."""
    booking = await book(store, config, now)
    starts = [
        fmt_hhmm(t)
        for t in await free_starts(
            store,
            config,
            config.specialist("spec1"),
            MONDAY,
            config.service("s90"),
            now,
            ignore=booking,
        )
    ]
    assert "12:00" in starts and "11:00" in starts


async def test_purge_wipes_day_beyond_history(store, config, now):
    """После срока хранения от дня не остаётся ни записи, ни замков, ни индексов."""
    booking = await book(store, config, now)
    much_later = now + timedelta(days=HISTORY_DAYS + 1)

    assert await purge_expired(store, config, much_later) == 1
    assert await day_bookings(store, MONDAY) == []
    assert await user_bookings(store, config, 1, now, upcoming_only=False) == []
    assert await store.get(f"booking:{booking.id}") is None
    assert await store.get(busy_key("spec1", MONDAY, time(12, 0))) is None
    assert await store.smembers(busyday_key("spec1", MONDAY)) == []


async def test_purge_keeps_fresh_days(store, config, now):
    """Вчерашний день ещё в пределах хранения — уборка его не трогает."""
    await book(store, config, now)
    assert await purge_expired(store, config, now + timedelta(days=1)) == 0
    assert len(await day_bookings(store, MONDAY)) == 1


async def test_keys_expire_on_their_own(store, config, now):
    """Даже без уборки ключи умирают сами: срок жизни ставится при создании."""
    booking = await book(store, config, now)
    data = store._path.read_text(encoding="utf-8")
    assert f"booking:{booking.id}" in data

    # Отматываем часы вперёд, подкручивая записанные в файле сроки жизни.
    raw = json.loads(data)
    shift = (HISTORY_DAYS + 2) * 24 * 60 * 60
    for section in ("exp", "sets_exp"):
        raw[section] = {key: until - shift for key, until in raw[section].items()}
    store._path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    assert await store.get(f"booking:{booking.id}") is None
    assert await store.smembers(f"day:{MONDAY.isoformat()}") == []
    assert await store.smembers(busyday_key("spec1", MONDAY)) == []
