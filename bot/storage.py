"""Хранилище записей.

Бот работает на бесплатном serverless-вебхуке, где процесс живёт доли секунды и
ничего не помнит между сообщениями. Поэтому состояние не держится в памяти: всё
складывается сюда.

Два движка с одинаковым интерфейсом:
* `UpstashStore` — Redis по HTTP (прод, Vercel). Обычный Redis по TCP в serverless
  неудобен: соединение не переживает заморозку функции.
* `JsonStore` — файл на диске (локальный запуск и тесты).

Нужен минимум команд: строки с TTL, атомарный SETNX (им занимается слот, чтобы двое
не записались на одно время) и множества (записи клиента, записи дня). Срок жизни
ставится и множествам — иначе прошедшие дни копились бы в хранилище вечно.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Protocol

import aiohttp


class Store(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    async def set_nx(self, key: str, value: str, ttl: int | None = None) -> bool: ...
    async def delete(self, *keys: str) -> int: ...
    async def expire(self, key: str, ttl: int) -> None: ...
    async def sadd(self, key: str, member: str) -> None: ...
    async def srem(self, key: str, member: str) -> None: ...
    async def smembers(self, key: str) -> list[str]: ...
    async def close(self) -> None: ...


_shared: aiohttp.ClientSession | None = None


def shared_session(token: str) -> aiohttp.ClientSession:
    """Одна HTTP-сессия на «тёплый» контейнер.

    Новая сессия — это заново TCP и TLS до Upstash, а контейнер на serverless
    переживает несколько апдейтов: сессию имеет смысл донести до следующего.
    Сессия привязана к циклу событий, в котором создана, поэтому цикл проверяем:
    если рантайм подсунул новый, старая непригодна и берём другую.
    """
    global _shared
    loop = asyncio.get_running_loop()
    if _shared is None or _shared.closed or getattr(_shared, "_loop", None) is not loop:
        _shared = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Authorization": f"Bearer {token}"},
        )
    return _shared


class UpstashStore:
    """Redis Upstash через REST: одна HTTP-команда — одна операция."""

    def __init__(
        self,
        url: str,
        token: str,
        session: aiohttp.ClientSession | None = None,
        *,
        shared: bool = False,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._session = session
        self._own_session = session is None and not shared
        self._shared = shared

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._shared:
            self._session = shared_session(self._token)
        else:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._own_session = True
        return self._session

    async def _cmd(self, *parts: str | int):
        session = await self._client()
        async with session.post(self._url, json=[str(p) for p in parts]) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if "error" in payload:
            raise RuntimeError(f"upstash: {payload['error']}")
        return payload.get("result")

    async def get(self, key: str) -> str | None:
        return await self._cmd("GET", key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            await self._cmd("SET", key, value, "EX", ttl)
        else:
            await self._cmd("SET", key, value)

    async def set_nx(self, key: str, value: str, ttl: int | None = None) -> bool:
        args = ["SET", key, value, "NX"]
        if ttl:
            args += ["EX", str(ttl)]
        return await self._cmd(*args) is not None

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return int(await self._cmd("DEL", *keys) or 0)

    async def expire(self, key: str, ttl: int) -> None:
        await self._cmd("EXPIRE", key, ttl)

    async def sadd(self, key: str, member: str) -> None:
        await self._cmd("SADD", key, member)

    async def srem(self, key: str, member: str) -> None:
        await self._cmd("SREM", key, member)

    async def smembers(self, key: str) -> list[str]:
        return list(await self._cmd("SMEMBERS", key) or [])

    async def close(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()


class JsonStore:
    """Тот же интерфейс поверх JSON-файла — для локального запуска и тестов."""

    def __init__(self, path: str | Path = "data/bookings.json") -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    def _read(self) -> dict:
        empty = {"kv": {}, "exp": {}, "sets": {}, "sets_exp": {}}
        if not self._path.exists():
            return empty
        data = empty | json.loads(self._path.read_text(encoding="utf-8"))
        now = time.time()
        for key in [k for k, until in data["exp"].items() if until <= now]:
            data["kv"].pop(key, None)
            data["exp"].pop(key, None)
        for key in [k for k, until in data["sets_exp"].items() if until <= now]:
            data["sets"].pop(key, None)
            data["sets_exp"].pop(key, None)
        return data

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    async def get(self, key: str) -> str | None:
        async with self._lock:
            return self._read()["kv"].get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        async with self._lock:
            data = self._read()
            data["kv"][key] = value
            if ttl:
                data["exp"][key] = time.time() + ttl
            else:
                data["exp"].pop(key, None)
            self._write(data)

    async def set_nx(self, key: str, value: str, ttl: int | None = None) -> bool:
        async with self._lock:
            data = self._read()
            if key in data["kv"]:
                return False
            data["kv"][key] = value
            if ttl:
                data["exp"][key] = time.time() + ttl
            self._write(data)
            return True

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            data = self._read()
            removed = 0
            for key in keys:
                # DEL в Redis не разбирает тип ключа — здесь тоже сносим и строку, и множество.
                if data["kv"].pop(key, None) is not None or data["sets"].pop(key, None) is not None:
                    removed += 1
                data["exp"].pop(key, None)
                data["sets_exp"].pop(key, None)
            self._write(data)
            return removed

    async def expire(self, key: str, ttl: int) -> None:
        async with self._lock:
            data = self._read()
            until = time.time() + ttl
            if key in data["kv"]:
                data["exp"][key] = until
            if key in data["sets"]:
                data["sets_exp"][key] = until
            self._write(data)

    async def sadd(self, key: str, member: str) -> None:
        async with self._lock:
            data = self._read()
            members = data["sets"].setdefault(key, [])
            if member not in members:
                members.append(member)
            self._write(data)

    async def srem(self, key: str, member: str) -> None:
        async with self._lock:
            data = self._read()
            members = data["sets"].get(key, [])
            if member in members:
                members.remove(member)
            self._write(data)

    async def smembers(self, key: str) -> list[str]:
        async with self._lock:
            return list(self._read()["sets"].get(key, []))

    async def close(self) -> None:
        return None


def create_store() -> Store:
    """Redis, если заданы его переменные, иначе локальный файл.

    Vercel подставляет имена по-разному в зависимости от того, как подключено
    хранилище — из маркетплейса Upstash или через встроенный KV, — поэтому смотрим оба.
    """
    url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")
    if url and token:
        # На проде сессия общая на контейнер: `close()` её не трогает, она нужна
        # следующему апдейту. Прямое создание `UpstashStore(...)` (тесты) — как было.
        return UpstashStore(url, token, shared=True)
    return JsonStore(os.getenv("LOCAL_STORE_PATH", "data/bookings.json"))
