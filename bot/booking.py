"""Операции над записями: что свободно, кто на что записан, бронь и отмена.

Хранилище — плоский Redis-подобный набор ключей:

* `booking:{id}`                  — сама запись (JSON);
* `busy:{spec}:{d}:{HH:MM}`       — замок на клетку расписания, ставится через SETNX;
* `busyday:{spec}:{d}`            — множество занятых клеток дня (одним запросом читаем день);
* `user:{tg_id}`                  — записи клиента;
* `day:{d}`                       — записи дня (для админа и напоминаний);
* `client:{tg_id}`                — имя и телефон, чтобы не спрашивать повторно;
* `pending:{tg_id}`               — выбранный, но не подтверждённый слот, живёт 15 минут.

Замок на клетку — то место, где решается гонка: если два человека одновременно жмут
одно время, SETNX ляжет только у первого, второй получит «время только что заняли».

Ключи прошедших дней никому не нужны, а чистить их некому: процесса, который бы
приглядывал за хранилищем, здесь нет. Поэтому каждому ключу при создании ставится
срок жизни — до конца дня визита плюс `HISTORY_DAYS`; вдобавок утренний cron проходит
по дням, вышедшим за этот срок, и сносит остатки (см. `purge_expired`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from .config import BookingConfig, Service, Specialist
from .schedule import busy_key, fmt_hhmm, parse_hhmm, is_bookable, shift_starts, slot_cells
from .storage import Store

log = logging.getLogger(__name__)

PENDING_TTL = 15 * 60
HISTORY_DAYS = 30  # сколько запись хранится после дня визита
PURGE_WINDOW_DAYS = 30  # сколько дней разом просматривает утренняя уборка
CLIENT_TTL = 365 * 24 * 60 * 60  # телефон клиента: год без визитов — и он забыт


@dataclass
class Booking:
    id: str
    user_id: int
    user_name: str
    phone: str
    username: str
    service_id: str
    specialist_id: str
    day: str  # YYYY-MM-DD
    start: str  # HH:MM
    created_at: str

    @property
    def date(self) -> date:
        return date.fromisoformat(self.day)

    @property
    def time(self) -> time:
        return parse_hhmm(self.start)

    def starts_at(self, config: BookingConfig) -> datetime:
        return datetime.combine(self.date, self.time, tzinfo=config.tz)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Booking | None":
        """None — запись записана другой версией бота и разобрать её нечем.

        Набор полей со временем меняется, а в хранилище остаются старые записи;
        падать из-за них на каждом `/today` бот не должен — такую запись он считает
        пропавшей, и индекс подчищается сам (см. `_collect`).
        """
        try:
            return cls(**json.loads(raw))
        except (json.JSONDecodeError, TypeError) as error:
            log.warning("не разобрал запись из хранилища: %s", error)
            return None


def busyday_key(specialist_id: str, day: date) -> str:
    return f"busyday:{specialist_id}:{day.isoformat()}"


def day_ttl(config: BookingConfig, day: date, now: datetime) -> int:
    """Сколько секунд осталось жить ключам этого дня."""
    dies_at = datetime.combine(day + timedelta(days=HISTORY_DAYS + 1), time(0, 0), tzinfo=config.tz)
    return max(int((dies_at - now).total_seconds()), 60)


async def busy_cells(store: Store, specialist_id: str, day: date) -> set[str]:
    return set(await store.smembers(busyday_key(specialist_id, day)))


def booking_cells(config: BookingConfig, booking: "Booking") -> set[str]:
    """Клетки расписания, которые занимает уже созданная запись."""
    service = config.service(booking.service_id)
    duration = service.duration if service else config.slot_step_minutes
    return {fmt_hhmm(cell) for cell in slot_cells(booking.time, duration, config.slot_step_minutes)}


async def free_starts(
    store: Store,
    config: BookingConfig,
    specialist: Specialist,
    day: date,
    service: Service,
    now: datetime,
    *,
    ignore: "Booking | None" = None,
) -> list[time]:
    """Время, когда этот специалист может взять эту услугу в этот день.

    `ignore` — запись, которую переносят: её собственные клетки заняты ею же, и без
    этого клиент не увидел бы ни своего времени, ни соседних окон рядом с ним.
    """
    taken = await busy_cells(store, specialist.id, day)
    if ignore is not None and ignore.specialist_id == specialist.id and ignore.day == day.isoformat():
        taken -= booking_cells(config, ignore)
    result = []
    for start in shift_starts(specialist, day, service, config):
        if not is_bookable(day, start, config, now):
            continue
        cells = slot_cells(start, service.duration, config.slot_step_minutes)
        if any(fmt_hhmm(cell) in taken for cell in cells):
            continue
        result.append(start)
    return result


async def free_starts_by_specialist(
    store: Store,
    config: BookingConfig,
    specialists: tuple[Specialist, ...],
    day: date,
    service: Service,
    now: datetime,
    *,
    ignore: "Booking | None" = None,
) -> dict[str, list[str]]:
    """{'12:00': ['spec1', 'spec3'], ...} — кто свободен в каждое время.

    Специалисты друг от друга не зависят, а каждое чтение занятости — поход по сети.
    На «любом специалисте» их столько же, сколько людей в конфиге, и по очереди это
    складывалось в заметную паузу перед экраном времени.
    """
    per_specialist = await asyncio.gather(
        *(
            free_starts(store, config, specialist, day, service, now, ignore=ignore)
            for specialist in specialists
        )
    )
    result: dict[str, list[str]] = {}
    for specialist, starts in zip(specialists, per_specialist):
        for start in starts:
            result.setdefault(fmt_hhmm(start), []).append(specialist.id)
    return dict(sorted(result.items()))


async def has_free_time(
    store: Store,
    config: BookingConfig,
    specialists: tuple[Specialist, ...],
    day: date,
    service: Service,
    now: datetime,
) -> bool:
    for specialist in specialists:
        if await free_starts(store, config, specialist, day, service, now):
            return True
    return False


async def create_booking(
    store: Store,
    config: BookingConfig,
    *,
    user_id: int,
    user_name: str,
    phone: str,
    username: str,
    service: Service,
    specialist: Specialist,
    day: date,
    start: time,
    now: datetime,
) -> Booking | None:
    """Занимает клетки расписания и сохраняет запись. None — время увели."""
    booking_id = uuid.uuid4().hex[:10]
    cells = slot_cells(start, service.duration, config.slot_step_minutes)
    ttl = day_ttl(config, day, now)

    # Замки на клетки независимы, поэтому ставятся разом. Гонку это не ослабляет:
    # атомарность у каждого SETNX своя, а если хоть один не лёг — снимаем только те,
    # что легли у нас, и уходим. Чужие замки не трогаем.
    locks = await asyncio.gather(
        *(store.set_nx(busy_key(specialist.id, day, cell), booking_id, ttl=ttl) for cell in cells)
    )
    if not all(locks):
        ours = [cell for cell, locked in zip(cells, locks) if locked]
        if ours:
            await store.delete(*(busy_key(specialist.id, day, cell) for cell in ours))
        return None

    booking = Booking(
        id=booking_id,
        user_id=user_id,
        user_name=user_name,
        phone=phone,
        username=username,
        service_id=service.id,
        specialist_id=specialist.id,
        day=day.isoformat(),
        start=fmt_hhmm(start),
        created_at=now.isoformat(timespec="seconds"),
    )
    await asyncio.gather(
        store.set(f"booking:{booking_id}", booking.to_json(), ttl=ttl),
        *(store.sadd(busyday_key(specialist.id, day), fmt_hhmm(cell)) for cell in cells),
        store.sadd(f"user:{user_id}", booking_id),
        store.sadd(f"day:{day.isoformat()}", booking_id),
    )
    # Множествам срок ставится после наполнения — отдельной волной: SADD по истёкшему
    # ключу создаёт его заново, уже без срока. У списка записей клиента отсчёт идёт
    # от последней записи.
    await asyncio.gather(
        store.expire(busyday_key(specialist.id, day), ttl),
        store.expire(f"day:{day.isoformat()}", ttl),
        store.expire(f"user:{user_id}", ttl),
    )
    return booking


async def move_booking(
    store: Store,
    config: BookingConfig,
    booking_id: str,
    *,
    day: date,
    start: time,
    now: datetime,
) -> Booking | None:
    """Переносит запись на другое время того же специалиста. None — время увели.

    Свои клетки заново не занимаем: при переносе «на полчаса вперёд» новая расстановка
    накрывает старую, и `SET NX` по собственному замку не прошёл бы. Всё, что осталось
    от прежнего времени, снимается только после того, как новое удалось занять.
    """
    booking = await get_booking(store, booking_id)
    service = config.service(booking.service_id) if booking else None
    if booking is None or service is None:
        return None

    old_day, old_cells = booking.date, booking_cells(config, booking)
    same_day = booking.day == day.isoformat()
    new_cells = slot_cells(start, service.duration, config.slot_step_minutes)
    ttl = day_ttl(config, day, now)

    locked: list[time] = []
    for cell in new_cells:
        if same_day and fmt_hhmm(cell) in old_cells:
            continue  # клетка и так занята этой же записью
        if await store.set_nx(busy_key(booking.specialist_id, day, cell), booking_id, ttl=ttl):
            locked.append(cell)
        else:
            await store.delete(*(busy_key(booking.specialist_id, day, c) for c in locked))
            return None

    stale = old_cells - ({fmt_hhmm(cell) for cell in new_cells} if same_day else set())
    await store.delete(
        *(busy_key(booking.specialist_id, old_day, parse_hhmm(label)) for label in stale)
    )
    for label in stale:
        await store.srem(busyday_key(booking.specialist_id, old_day), label)
    for cell in new_cells:
        await store.sadd(busyday_key(booking.specialist_id, day), fmt_hhmm(cell))
    await store.expire(busyday_key(booking.specialist_id, day), ttl)

    if not same_day:
        await store.srem(f"day:{booking.day}", booking_id)
        await store.sadd(f"day:{day.isoformat()}", booking_id)
        await store.expire(f"day:{day.isoformat()}", ttl)

    booking.day = day.isoformat()
    booking.start = fmt_hhmm(start)
    await store.set(f"booking:{booking_id}", booking.to_json(), ttl=ttl)
    await store.expire(f"user:{booking.user_id}", ttl)
    return booking


async def get_booking(store: Store, booking_id: str) -> Booking | None:
    raw = await store.get(f"booking:{booking_id}")
    return Booking.from_json(raw) if raw else None


async def cancel_booking(store: Store, config: BookingConfig, booking_id: str) -> Booking | None:
    booking = await get_booking(store, booking_id)
    if booking is None:
        return None
    service = config.service(booking.service_id)
    duration = service.duration if service else config.slot_step_minutes
    cells = slot_cells(booking.time, duration, config.slot_step_minutes)

    await store.delete(*(busy_key(booking.specialist_id, booking.date, c) for c in cells))
    for cell in cells:
        await store.srem(busyday_key(booking.specialist_id, booking.date), fmt_hhmm(cell))
    await store.srem(f"user:{booking.user_id}", booking_id)
    await store.srem(f"day:{booking.day}", booking_id)
    await store.delete(f"booking:{booking_id}")
    return booking


async def _collect(store: Store, index_key: str) -> list[Booking]:
    ids = await store.smembers(index_key)
    loaded = await asyncio.gather(*(get_booking(store, booking_id) for booking_id in ids))
    missing = [booking_id for booking_id, booking in zip(ids, loaded) if booking is None]
    if missing:  # записи пропали — чистим индекс
        await asyncio.gather(*(store.srem(index_key, booking_id) for booking_id in missing))
    return sorted((b for b in loaded if b is not None), key=lambda b: (b.day, b.start))


async def user_bookings(
    store: Store, config: BookingConfig, user_id: int, now: datetime, *, upcoming_only: bool = True
) -> list[Booking]:
    bookings = await _collect(store, f"user:{user_id}")
    if not upcoming_only:
        return bookings
    return [b for b in bookings if b.starts_at(config) >= now]


async def day_bookings(store: Store, day: date) -> list[Booking]:
    return await _collect(store, f"day:{day.isoformat()}")


# --- уборка прошедших дней --------------------------------------------------


async def purge_day(store: Store, config: BookingConfig, day: date) -> int:
    """Сносит всё, что осталось от дня: записи, замки клеток и индексы."""
    bookings = await day_bookings(store, day)
    for booking in bookings:
        service = config.service(booking.service_id)
        duration = service.duration if service else config.slot_step_minutes
        cells = slot_cells(booking.time, duration, config.slot_step_minutes)
        await store.delete(*(busy_key(booking.specialist_id, day, cell) for cell in cells))
        await store.srem(f"user:{booking.user_id}", booking.id)
        await store.delete(f"booking:{booking.id}")
    await store.delete(
        f"day:{day.isoformat()}",
        *(busyday_key(specialist.id, day) for specialist in config.specialists),
    )
    return len(bookings)


async def purge_expired(store: Store, config: BookingConfig, now: datetime) -> int:
    """Убирает дни, вышедшие за срок хранения. Возвращает число снесённых записей.

    Срок жизни ключей делает то же самое сам, но он есть не у всех: записи, созданные
    до его появления, живут вечно, а `SADD` в уже истёкшее множество воскрешает его без
    срока. Поэтому раз в сутки проходим окно дней перед границей хранения — так
    подчищается и старое, и то, что осталось от дней, когда уборка не запускалась.
    """
    cutoff = now.astimezone(config.tz).date() - timedelta(days=HISTORY_DAYS)
    purged = 0
    for offset in range(PURGE_WINDOW_DAYS):
        purged += await purge_day(store, config, cutoff - timedelta(days=offset))
    return purged


# --- профиль клиента и незавершённый выбор ---------------------------------


async def save_client(store: Store, user_id: int, name: str, phone: str) -> None:
    await store.set(
        f"client:{user_id}",
        json.dumps({"name": name, "phone": phone}, ensure_ascii=False),
        ttl=CLIENT_TTL,
    )


async def get_client(store: Store, user_id: int) -> dict | None:
    raw = await store.get(f"client:{user_id}")
    return json.loads(raw) if raw else None


async def save_pending(store: Store, user_id: int, payload: dict) -> None:
    await store.set(f"pending:{user_id}", json.dumps(payload, ensure_ascii=False), ttl=PENDING_TTL)


async def pop_pending(store: Store, user_id: int) -> dict | None:
    raw = await store.get(f"pending:{user_id}")
    if raw is None:
        return None
    await store.delete(f"pending:{user_id}")
    return json.loads(raw)
