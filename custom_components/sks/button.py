"""Button entity submitting pending meter readings."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SksConfigEntry, SksCoordinator
from .sensor import _counter_name

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SksConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание кнопки «Передать показания» на каждый счётчик."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    buttons = [
        SksSubmitButton(coordinator, account_id, counter)
        for counter in data.get("counters") or []
    ]
    async_add_entities(buttons)


class SksSubmitButton(CoordinatorEntity[SksCoordinator], ButtonEntity):
    """«Передать показания» — POSTs the whole assembly's pending values.

    The site submits each «Группа приборов учёта» form at once (inputs
    are required), so pressing a meter's button sends the pending values
    of every meter in the same assembly — just like the web form does.
    Unavailable while nothing was typed into the meter's input.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:send-check"

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
        self._attr_name = "Передать показания"
        self._attr_unique_id = (
            f"{DOMAIN}_{account_id}_{self._counter_id}_submit"
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
    def available(self) -> bool:
        return (
            super().available
            and self._counter_id in self.coordinator.pending
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"pending": self.coordinator.pending.get(self._counter_id)}

    async def async_press(self) -> None:
        """POST pending values of every meter in this meter's assembly."""
        pending = dict(self.coordinator.pending)
        if self._counter_id not in pending:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_pending_readings",
            )
        assembly_id = self._counter().get("assembly_id")
        if assembly_id is not None:
            siblings = set(self.coordinator.assembly_counters(assembly_id))
            readings = {
                cid: v for cid, v in pending.items() if cid in siblings
            }
        else:
            readings = {self._counter_id: pending[self._counter_id]}
        await self.coordinator.async_submit_readings(readings)
