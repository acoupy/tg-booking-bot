"""Проверка прод-хранилища.

На Vercel данные идут через Redis по HTTP, и это единственный путь, который не
проверяется остальными тестами. Поднимаем игрушечный сервер, отвечающий как Upstash,
и гоняем через него тот же сценарий записи.
"""

from datetime import date, datetime, time, timedelta

import pytest
from aiohttp import web

from bot.booking import (
    HISTORY_DAYS,
    busyday_key,
    cancel_booking,
    create_booking,
    free_starts,
    purge_expired,
    user_bookings,
)
from bot.config import get_config
from bot.schedule import busy_key, fmt_hhmm
from bot.storage import UpstashStore, create_store

MONDAY = date(2026, 8, 3)


class FakeRedis:
    def __init__(self):
        self.strings: dict[str, str] = {}
        self.sets: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}  # сроки жизни не тикают, но видно, что они выставлены

    def execute(self, command: list[str]):
        name, args = command[0].upper(), command[1:]
        if name == "GET":
            return self.strings.get(args[0])
        if name == "SET":
            key, value, *options = args
            options = [o.upper() for o in options]
            if "NX" in options and key in self.strings:
                return None
            self.strings[key] = value
            if "EX" in options:
                self.ttl[key] = int(options[options.index("EX") + 1])
            return "OK"
        if name == "DEL":
            return sum(
                1
                for key in args
                if self.strings.pop(key, None) is not None or self.sets.pop(key, None) is not None
            )
        if name == "EXPIRE":
            key, ttl = args[0], int(args[1])
            if key not in self.strings and key not in self.sets:
                return 0
            self.ttl[key] = ttl
            return 1
        if name == "SADD":
            members = self.sets.setdefault(args[0], [])
            if args[1] not in members:
                members.append(args[1])
            return 1
        if name == "SREM":
            members = self.sets.get(args[0], [])
            if args[1] in members:
                members.remove(args[1])
                return 1
            return 0
        if name == "SMEMBERS":
            return list(self.sets.get(args[0], []))
        raise AssertionError(f"хранилище прислало неизвестную команду: {name}")


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
async def store(aiohttp_server, redis):
    async def handle(request: web.Request):
        if request.headers.get("Authorization") != "Bearer test-token":
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"result": redis.execute(await request.json())})

    app = web.Application()
    app.router.add_post("/", handle)
    server = await aiohttp_server(app)
    store = UpstashStore(str(server.make_url("/")), "test-token")
    yield store
    await store.close()


@pytest.fixture
def config():
    return get_config()


def test_store_choice_by_env(monkeypatch, tmp_path):
    """Без переменных Redis — файл; с любым из двух вариантов имён — Redis."""
    from bot.storage import JsonStore, UpstashStore, create_store

    for name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
                 "KV_REST_API_URL", "KV_REST_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCAL_STORE_PATH", str(tmp_path / "b.json"))
    assert isinstance(create_store(), JsonStore)

    monkeypatch.setenv("KV_REST_API_URL", "https://example.upstash.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "token")
    assert isinstance(create_store(), UpstashStore)


async def test_basic_commands(store):
    assert await store.get("missing") is None
    await store.set("k", "v")
    assert await store.get("k") == "v"
    assert await store.set_nx("k", "other") is False
    assert await store.set_nx("fresh", "v") is True
    await store.sadd("s", "a")
    await store.sadd("s", "a")
    await store.sadd("s", "b")
    assert sorted(await store.smembers("s")) == ["a", "b"]
    await store.srem("s", "a")
    assert await store.smembers("s") == ["b"]
    await store.expire("s", 60)
    assert await store.delete("k") == 1
    assert await store.delete("s") == 1  # DEL сносит и множества, на этом держится уборка


async def test_booking_cycle_over_http(store, config):
    now = datetime(2026, 8, 3, 9, 0, tzinfo=config.tz)
    booking = await create_booking(
        store,
        config,
        user_id=5,
        user_name="Тест",
        phone="+79000000000",
        username="test",
        service=config.service("s90"),
        specialist=config.specialist("spec1"),
        day=MONDAY,
        start=time(12, 0),
        now=now,
    )
    assert booking is not None

    free = [fmt_hhmm(t) for t in await free_starts(
        store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
    )]
    assert "12:00" not in free and "13:00" not in free
    assert [b.id for b in await user_bookings(store, config, 5, now)] == [booking.id]

    await cancel_booking(store, config, booking.id)
    free = [fmt_hhmm(t) for t in await free_starts(
        store, config, config.specialist("spec1"), MONDAY, config.service("s90"), now
    )]
    assert "12:00" in free


async def test_every_key_of_a_booking_gets_ttl(store, redis, config):
    """Записи не должны лежать в Redis вечно — срок жизни ставится всем ключам сразу."""
    now = datetime(2026, 8, 3, 9, 0, tzinfo=config.tz)
    booking = await create_booking(
        store,
        config,
        user_id=5,
        user_name="Тест",
        phone="+79000000000",
        username="test",
        service=config.service("s60"),
        specialist=config.specialist("spec1"),
        day=MONDAY,
        start=time(12, 0),
        now=now,
    )
    expected = {
        f"booking:{booking.id}",
        busy_key("spec1", MONDAY, time(12, 0)),
        busyday_key("spec1", MONDAY),
        f"day:{MONDAY.isoformat()}",
        "user:5",
    }
    assert expected <= redis.ttl.keys()
    assert all(redis.ttl[key] > 0 for key in expected)


async def test_purge_clears_redis(store, redis, config):
    """Уборка идёт теми же командами, что и на проде: DEL по строкам и по множествам."""
    now = datetime(2026, 8, 3, 9, 0, tzinfo=config.tz)
    await create_booking(
        store,
        config,
        user_id=5,
        user_name="Тест",
        phone="+79000000000",
        username="test",
        service=config.service("s60"),
        specialist=config.specialist("spec1"),
        day=MONDAY,
        start=time(12, 0),
        now=now,
    )
    assert await purge_expired(store, config, now + timedelta(days=HISTORY_DAYS + 1)) == 1
    assert [k for k in redis.strings if k.startswith(("booking:", "busy:"))] == []
    assert [k for k in redis.sets if k.startswith(("day:", "busyday:"))] == []


async def test_prod_store_keeps_session_between_updates(monkeypatch):
    """На «тёплом» контейнере сессия переживает апдейт.

    Каждая новая сессия — это заново TCP и TLS до Upstash: на serverless контейнер
    живёт дольше одного апдейта, и рвать соединение после каждого нажатия незачем.
    Сессия смотрится изнутри намеренно — снаружи её не видно, а проверять надо
    именно её переиспользование.
    """
    monkeypatch.setattr("bot.storage._shared", None)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.invalid")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "test-token")

    first = create_store()
    session = await first._client()
    await first.close()
    assert not session.closed, "close() не должен рвать общую сессию"

    second = create_store()
    assert await second._client() is session
    await session.close()


async def test_direct_store_owns_its_session():
    """Созданный руками магазин — сам себе хозяин: закрыли и закрыли."""
    store = UpstashStore("https://example.invalid", "test-token")
    session = await store._client()
    await store.close()
    assert session.closed
