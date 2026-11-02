"""Sensors exposing account balance, meters and monthly accruals."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SksConfigEntry, SksCoordinator
from .parser import parse_ru_date

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

RUB = "₽"
M3 = UnitOfVolume.CUBIC_METERS


def _float(value: Any) -> float | None:
    """Convert stringy numbers ("177.48") to float."""
    try:
        return float(str(value).replace(",", ".").replace(" ", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort parse of scraped date values."""
    if isinstance(value, datetime):
        return dt_util.as_local(value)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if parsed := dt_util.parse_datetime(text):
        return parsed if parsed.tzinfo else dt_util.as_local(parsed)
    if parsed := parse_ru_date(text):
        return dt_util.as_local(parsed)
    return None


def _naive(value: Any) -> datetime:
    dt = _parse_dt(value)
    return dt.replace(tzinfo=None) if dt else datetime.min


def _latest(items: list[dict[str, Any]], field: str = "date") -> dict[str, Any]:
    return max(items, key=lambda i: _naive(i.get(field) or i.get("_dt")), default={})


def _iso(value: Any) -> str | None:
    dt = _parse_dt(value)
    return dt.date().isoformat() if dt else None


def _counter_name(counter: dict[str, Any]) -> str:
    """«Холодное водоснабжение №140541143» — serial distinguishes same-name meters."""
    name = counter.get("name") or "Счётчик"
    serial = counter.get("serial")
    return f"{name} №{serial}" if serial else name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание сенсоров SKS для лицевого счёта."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    sensors: list[SensorEntity] = [
        SksBalanceSensor(coordinator, account_id),
        SksChargedSensor(coordinator, account_id),
        SksConsumptionSensor(coordinator, account_id),
        SksNotificationsSensor(coordinator, account_id),
        SksLastPaymentSensor(coordinator, account_id),
    ]
    for counter in data.get("counters") or []:
        sensors.append(SksCounterSensor(coordinator, account_id, counter))
        sensors.append(SksCounterRateSensor(coordinator, account_id, counter))
        if counter.get("check_date"):
            sensors.append(SksCounterCheckDateSensor(coordinator, account_id, counter))
    if data.get("accrual_current"):
        sensors.append(SksPriceSensor(coordinator, account_id))

    async_add_entities(sensors)
    _LOGGER.debug("Создано %d сенсоров SKS", len(sensors))


class SksBaseSensor(CoordinatorEntity[SksCoordinator], SensorEntity):
    """Базовый класс — доступ к данным координатора."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self.account_id = account_id
        super().__init__(coordinator)

    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    def _account(self) -> dict[str, Any]:
        return self._data().get("account") or {}

    def _accruals(self) -> list[dict[str, Any]]:
        return [a for a in self._data().get("accruals") or [] if isinstance(a, dict)]

    def _accrual_current_rows(self) -> list[dict[str, Any]]:
        return [
            a
            for a in self._data().get("accrual_current") or []
            if isinstance(a, dict)
        ]

    def _accrual_current(self) -> dict[str, Any]:
        """Первый ряд текущей детализации (услуга/тариф/объём за месяц)."""
        rows = self._accrual_current_rows()
        return rows[0] if rows else {}

    def _payments(self) -> list[dict[str, Any]]:
        return [
            p
            for p in self._data().get("payments") or []
            if isinstance(p, dict)
        ]

    def _counter(self, counter_id: str) -> dict[str, Any]:
        for counter in self._data().get("counters") or []:
            if str(counter.get("id")) == counter_id:
                return counter
        return {}

    @staticmethod
    def _tariff_price(tariff: Any) -> float | None:
        """First decimal in a tariff cell — "58.56" or «Одноставочный (58.56 ₽)»."""
        if m := re.search(r"(\d+[.,]\d+)", str(tariff or "")):
            return _float(m.group(1))
        return None

    def _service_prices(self) -> dict[str, float]:
        """All current-period service tariffs: {"ХВС": 58.56, "Водоотведение ХВ": 35.40}."""
        prices: dict[str, float] = {}
        for row in self._accrual_current_rows():
            service = row.get("service")
            price = self._tariff_price(row.get("tariff"))
            if service and price is not None:
                prices[str(service)] = price
        return prices

    _WATER_TYPES = ("ХВС", "ГВС")

    def _water_prices(self) -> dict[str, float]:
        """Effective ₽/m³ per water type: supply tariff + matching disposal.

        Every cubic metre is billed twice — «водоснабжение» and
        «водоотведение» at different tariffs — so the real price of water
        is their sum: {"ХВС": 93.96, "ГВС": 93.96}.
        """
        supply: dict[str, float] = {}
        disposal: dict[str, float] = {}
        for row in self._accrual_current_rows():
            service = str(row.get("service") or "")
            low = service.lower()
            if not any(
                k in low for k in ("водоснабжен", "водоотведен", "хвс", "гвс")
            ):
                continue
            # «ХВС на ГВС» / «Водоотведение ГВ» belong to the hot-water bundle.
            wtype = "ГВС" if "гв" in low or "горяч" in low else "ХВС"
            price = self._tariff_price(row.get("tariff"))
            if price is None:
                continue
            bucket = disposal if "водоотведен" in low else supply
            bucket[wtype] = bucket.get(wtype, 0.0) + price
        return {
            t: round(supply.get(t, 0.0) + disposal.get(t, 0.0), 2)
            for t in self._WATER_TYPES
            if t in supply or t in disposal
        }

    def _account_device_info(self) -> dict[str, Any]:
        account = self._account()
        number = account.get("number") or self.account_id
        return {
            "identifiers": {(DOMAIN, self.account_id)},
            "name": f"СКС {number}",
            "manufacturer": "Российские коммунальные системы",
            "model": account.get("address"),
        }


class SksAccountSensor(SksBaseSensor):
    """Сенсор уровня лицевого счёта — живёт на устройстве ЛС."""

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        super().__init__(coordinator, account_id)
        self._attr_device_info = self._account_device_info()


class SksBalanceSensor(SksAccountSensor):
    """Текущая сумма к оплате по лицевому счёту."""

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_balance"
        self._attr_name = "К оплате"
        self._attr_icon = "mdi:currency-rub"
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        return _float(self._account().get("balance"))

    @property
    def extra_state_attributes(self):
        account = self._account()
        return {
            "account": account.get("number"),
            "address": account.get("address"),
            "owner": account.get("owner"),
            "email": account.get("email"),
            "accounts": account.get("accounts"),
            "debt_start": _float(account.get("debt_start")),
            "charged": _float(account.get("charged_current")),
            "paid": _float(account.get("paid_current")),
            "recalc": _float(account.get("recalc")),
            "penalty": _float(account.get("penalty")),
            "prices": self._service_prices(),
            "prices_total": self._water_prices(),
        }


class SksNotificationsSensor(SksAccountSensor):
    """Непрочитанные уведомления от поставщика."""

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_notifications"
        self._attr_name = "Уведомления"
        self._attr_icon = "mdi:bell"
        super().__init__(coordinator, account_id)

    def _items(self) -> list[dict[str, Any]]:
        return [
            n
            for n in self._data().get("notifications") or []
            if isinstance(n, dict)
        ]

    @property
    def native_value(self):
        return self._data().get("unread") or 0

    @property
    def extra_state_attributes(self):
        items = self._items()
        last = _latest(items)
        return {
            "total": len(items),
            "last_date": _iso(last.get("date")),
            "last_text": (last.get("text") or "")[:255] or None,
            "notifications": [
                {
                    "id": n.get("id"),
                    "date": _iso(n.get("date")),
                    "text": (n.get("text") or "")[:255],
                    "unread": bool(n.get("unread")),
                }
                for n in items[:24]
            ],
        }


class SksChargedSensor(SksAccountSensor):
    """Сумма начислений за последний расчётный период."""

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_charged"
        self._attr_name = "Начислено"
        self._attr_icon = "mdi:receipt-text"
        # monetary + total — доступен как «entity tracking the total costs»
        # в дашборде (его статистика бэкфиллится помесячными суммами).
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    def _last(self) -> dict[str, Any]:
        return _latest(self._accruals())

    @property
    def native_value(self):
        return _float(self._last().get("charged"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        current = self._accrual_current()
        return {
            "period": last.get("period"),
            "date": _iso(last.get("_dt") or last.get("date")),
            "paid": _float(last.get("paid")),
            "balance_in": _float(last.get("balance_in")),
            "balance": _float(last.get("balance")),
            "service": current.get("service"),
            "tariff": current.get("tariff"),
            "recalc": _float(current.get("recalc")),
            "quality": _float(current.get("quality")),
            "services": [
                {
                    "service": r.get("service"),
                    "tariff": r.get("tariff"),
                    "volume": _float(r.get("volume")),
                    "charged": _float(r.get("charged")),
                    "paid": _float(r.get("paid")),
                    "balance": _float(r.get("balance")),
                }
                for r in self._accrual_current_rows()[:16]
            ],
            "accruals": [
                {
                    "period": a.get("period"),
                    "date": _iso(a.get("_dt") or a.get("date")),
                    "charged": _float(a.get("charged")),
                    "paid": _float(a.get("paid")),
                    "balance_in": _float(a.get("balance_in")),
                    "balance": _float(a.get("balance")),
                }
                for a in self._accruals()[:24]
            ],
        }


class SksLastPaymentSensor(SksAccountSensor):
    """Сумма последнего платежа."""

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_last_payment"
        self._attr_name = "Последний платёж"
        self._attr_icon = "mdi:cash-check"
        self._attr_native_unit_of_measurement = RUB
        super().__init__(coordinator, account_id)

    def _last(self) -> dict[str, Any]:
        return _latest(self._payments())

    @property
    def native_value(self):
        return _float(self._last().get("amount"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        return {
            "date": _iso(last.get("date")),
            "source": last.get("source"),
            "payments": [
                {
                    "date": _iso(p.get("date")),
                    "amount": _float(p.get("amount")),
                    "source": p.get("source"),
                }
                for p in self._payments()[:24]
            ],
        }


class SksConsumptionSensor(SksAccountSensor):
    """Расход воды за последний расчётный период, м³.

    The current-accruals table lists every service (ХВС, водоотведение,
    ХВС на ГВС …) with its own volume — only water-supply rows are summed,
    otherwise water disposal would double-count the same cubic metres.
    """

    _WATER_KEYWORDS = ("водоснабжен", "хвс", "гвс")

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_consumption"
        self._attr_name = "Расход за месяц"
        self._attr_icon = "mdi:water"
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = M3
        super().__init__(coordinator, account_id)

    def _water_rows(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self._accrual_current_rows()
            if any(k in str(r.get("service") or "").lower() for k in self._WATER_KEYWORDS)
        ]

    @property
    def native_value(self):
        rows = self._water_rows() or self._accrual_current_rows()[:1]
        total = sum(
            v for v in (_float(r.get("volume")) for r in rows) if v is not None
        )
        return total if rows else None

    @property
    def extra_state_attributes(self):
        rows = self._accrual_current_rows()
        return {
            "period": _latest(self._accruals()).get("period"),
            "services": [
                {
                    "service": r.get("service"),
                    "tariff": r.get("tariff"),
                    "volume": _float(r.get("volume")),
                    "charged": _float(r.get("charged")),
                }
                for r in rows[:16]
            ],
        }


class SksPriceSensor(SksAccountSensor):
    """Цена за м³ из текущего начисления («Одноставочный (86.40 ₽)»).

    state_class=MEASUREMENT — сенсор можно выбрать как «entity with the
    current price» в дашборде энергопотребления.
    """

    def __init__(self, coordinator: SksCoordinator, account_id: str):
        self._attr_unique_id = f"{DOMAIN}_{account_id}_price"
        self._attr_name = "Стоимость"
        self._attr_icon = "mdi:tag"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = f"{RUB}/м³"
        super().__init__(coordinator, account_id)

    @property
    def native_value(self):
        """Итоговая цена за м³ (подача + отведение); ХВС preferred as primary."""
        prices = self._water_prices()
        if prices:
            return next(
                (prices[t] for t in self._WATER_TYPES if t in prices),
                next(iter(prices.values())),
            )
        return self._tariff_price(self._accrual_current().get("tariff"))

    @property
    def extra_state_attributes(self):
        return {
            "tariff": self._accrual_current().get("tariff"),
            "prices": self._service_prices(),
            "prices_total": self._water_prices(),
        }


class SksCounterSensor(SksBaseSensor):
    """Последние показания прибора учёта — совместим с water dashboard."""

    def __init__(
        self, coordinator: SksCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{account_id}_{self._counter_id}_reading"
        self._attr_name = _counter_name(counter)
        self._attr_icon = "mdi:counter"
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = M3
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{account_id}_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
            "name": self._attr_name,
            "manufacturer": "СКС",
            "model": counter.get("model") or counter.get("serial"),
            "serial_number": counter.get("serial"),
        }

    def _c(self) -> dict[str, Any]:
        return self._counter(self._counter_id)

    @property
    def native_value(self):
        return _float(self._c().get("reading"))

    @property
    def extra_state_attributes(self):
        counter = self._c()
        return {
            "serial": counter.get("serial"),
            "group": counter.get("group"),
            "zones": counter.get("zones"),
            "reading_date": _iso(counter.get("reading_date")),
            "consumption": _float(counter.get("consumption")),
            "check_date": _iso(counter.get("check_date")),
            "history": [
                {
                    "date": _iso(h.get("date")),
                    "period": h.get("period"),
                    "reading": _float(h.get("reading")),
                    "volume": _float(h.get("volume")),
                    "unit": h.get("unit"),
                    "source": h.get("source"),
                }
                for h in counter.get("history") or []
            ],
        }


class SksCounterRateSensor(SksBaseSensor):
    """Расход за последний расчётный период (колонка «Расход» истории)."""

    def __init__(
        self, coordinator: SksCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{account_id}_{self._counter_id}_rate"
        self._attr_name = f"{_counter_name(counter)} — расход за период"
        self._attr_icon = "mdi:water"
        self._attr_device_class = SensorDeviceClass.WATER
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = M3
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{account_id}_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
        }

    def _last(self) -> dict[str, Any]:
        history = self._counter(self._counter_id).get("history") or []
        return history[0] if history else {}

    @property
    def native_value(self):
        return _float(self._last().get("volume"))

    @property
    def extra_state_attributes(self):
        last = self._last()
        return {
            "date": _iso(last.get("date")),
            "period": last.get("period"),
            "reading": _float(last.get("reading")),
            "source": last.get("source"),
        }


class SksCounterCheckDateSensor(SksBaseSensor):
    """Дата следующей поверки прибора учёта."""

    def __init__(
        self, coordinator: SksCoordinator, account_id: str, counter: dict[str, Any]
    ):
        self._counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        self._attr_unique_id = f"{DOMAIN}_{account_id}_{self._counter_id}_check_date"
        self._attr_name = f"{_counter_name(counter)} — поверка"
        self._attr_icon = "mdi:calendar-check"
        self._attr_device_class = SensorDeviceClass.DATE
        super().__init__(coordinator, account_id)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{account_id}_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
        }

    @property
    def native_value(self):
        dt = _parse_dt(self._counter(self._counter_id).get("check_date"))
        return dt.date() if dt else None
