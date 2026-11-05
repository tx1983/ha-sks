"""Number entities for entering new meter readings.

One box per meter (water meters are single-rate — zone [0]). Typing a
value does not send it immediately — it is kept as «pending» and is
POSTed together with the other meters of the same assembly when its
«Передать показания» button is pressed, mirroring the cabinet's form.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SksConfigEntry, SksCoordinator
from .sensor import _counter_name, _float, _iso

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание полей ввода показаний по каждому счётчику."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    numbers = [
        SksReadingNumber(coordinator, account_id, counter)
        for counter in data.get("counters") or []
    ]
    async_add_entities(numbers)
    _LOGGER.debug("Создано %d полей ввода показаний SKS", len(numbers))


class SksReadingNumber(CoordinatorEntity[SksCoordinator], NumberEntity):
    """Input box for a new reading of one meter."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_native_min_value = 0
    _attr_native_max_value = 99999.999
    _attr_native_step = 0.001
    _attr_mode = "box"
    _attr_icon = "mdi:water"

    def __init__(
        self,
        coordinator: SksCoordinator,
        account_id: str,
        counter: dict[str, Any],
    ):
        super().__init__(coordinator)
        self.account_id = account_id
        self._counter_id = str(
            counter.get("id") or counter.get("serial") or "meter"
        )
        self._attr_name = "Новое показание"
        self._attr_unique_id = (
            f"{DOMAIN}_{account_id}_{self._counter_id}_new_reading"
        )
        self._attr_device_info = {
            "identifiers": {
                (DOMAIN, f"counter_{account_id}_{self._counter_id}")
            },
            "via_device": (DOMAIN, self.account_id),
            "name": _counter_name(counter),
            "manufacturer": "СКС",
            "model": counter.get("model") or counter.get("serial"),
            "serial_number": counter.get("serial"),
        }

    def _counter(self) -> dict[str, Any]:
        return self.coordinator.counter(self._counter_id)

    @property
    def native_value(self) -> float | None:
        """Typed-in value if pending, else the last known reading."""
        if self._counter_id in self.coordinator.pending:
            return self.coordinator.pending[self._counter_id]
        return _float(self._counter().get("reading"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counter = self._counter()
        return {
            "pending": self.coordinator.pending.get(self._counter_id),
            "serial": counter.get("serial"),
            "group": counter.get("group"),
            "assembly_id": counter.get("assembly_id"),
            "last_reading": _float(counter.get("reading")),
            "last_reading_date": _iso(counter.get("reading_date")),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Keep the value as pending — the button submits it."""
        self.coordinator.set_pending(self._counter_id, value)
