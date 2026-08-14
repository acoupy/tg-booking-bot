"""Настройки записи и окружения.

Всё, что зависит от конкретного бизнеса, лежит в `config.json`: название, услуги,
специалисты, график, глубина записи. Код к предметной области не привязан — он умеет
«услуга занимает N минут у такого-то специалиста в такие-то часы», и этого достаточно
и для салона, и для автосервиса, и для репетитора.

Обязательны только `services` и `specialists`. Название, контакты и цены
необязательны: чего нет в конфиге, того не будет и в сообщениях.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(__file__).with_name("config.json")

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Service:
    id: str
    title: str
    duration: int  # минуты
    price: int = 0  # 0 — цену не показываем: не в каждом деле её называют заранее

    @property
    def price_label(self) -> str:
        if not self.price:
            return ""
        return f"{self.price:,}".replace(",", " ") + " ₽"


@dataclass(frozen=True)
class Specialist:
    id: str
    name: str
    services: tuple[str, ...]
    schedule: dict[str, tuple[str, str] | None]

    def works_on(self, weekday: int) -> tuple[str, str] | None:
        """weekday — как в `datetime.weekday()`: 0 = понедельник."""
        return self.schedule.get(WEEKDAY_KEYS[weekday])

    def does(self, service_id: str) -> bool:
        return service_id in self.services


@dataclass(frozen=True)
class BookingConfig:
    services: tuple[Service, ...]
    specialists: tuple[Specialist, ...]
    title: str = "Онлайн-запись"
    about: str = ""
    address: str = ""
    phone: str = ""
    site: str = ""
    timezone: str = "Europe/Moscow"
    booking_depth_days: int = 14
    slot_step_minutes: int = 30
    min_lead_minutes: int = 60
    max_active_bookings: int = 3  # сколько записей клиент может держать одновременно

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def contacts(self) -> tuple[tuple[str, str], ...]:
        """Заполненные контакты со значками — пустые строки в сообщения не попадают."""
        pairs = (("📍", self.address), ("☎️", self.phone), ("🌐", self.site))
        return tuple((icon, value) for icon, value in pairs if value)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def specialist(self, specialist_id: str) -> Specialist | None:
        return next((s for s in self.specialists if s.id == specialist_id), None)

    def specialists_for(self, service_id: str) -> tuple[Specialist, ...]:
        return tuple(s for s in self.specialists if s.does(service_id))


def load_config(path: Path | None = None) -> BookingConfig:
    raw = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    services = tuple(
        Service(
            id=s["id"],
            title=s["title"],
            duration=int(s["duration"]),
            price=int(s.get("price", 0)),
        )
        for s in raw["services"]
    )
    known = {s.id for s in services}
    specialists = []
    for item in raw["specialists"]:
        unknown = set(item["services"]) - known
        if unknown:
            raise ValueError(f"специалист {item['id']}: неизвестные услуги {sorted(unknown)}")
        schedule = {
            key: (tuple(item["schedule"][key]) if item["schedule"].get(key) else None)
            for key in WEEKDAY_KEYS
        }
        specialists.append(
            Specialist(
                id=item["id"],
                name=item["name"],
                services=tuple(item["services"]),
                schedule=schedule,
            )
        )
    optional = {
        name: raw[name]
        for name in ("title", "about", "address", "phone", "site", "timezone")
        if raw.get(name)
    }
    numbers = {
        name: int(raw[name])
        for name in (
            "booking_depth_days",
            "slot_step_minutes",
            "min_lead_minutes",
            "max_active_bookings",
        )
        if raw.get(name)
    }
    return BookingConfig(
        services=services,
        specialists=tuple(specialists),
        **optional,
        **numbers,
    )


@lru_cache(maxsize=1)
def get_config() -> BookingConfig:
    return load_config()


def admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(part) for part in raw.replace(";", ",").split(",") if part.strip().isdigit()}
