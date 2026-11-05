"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SksAuthError, SksClient, SksError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SksCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches the account summary, counters and accruals pages."""

    def __init__(
        self, hass: HomeAssistant, entry: SksConfigEntry, client: SksClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                )
            ),
        )
        self.client = client
        # Values typed into the reading NumberEntities, waiting for the
        # submit button: {counter_id: value} (counter_id == serial).
        self.pending: dict[str, float] = {}

    def set_pending(self, counter_id: str, value: float) -> None:
        """Store a typed-in reading and refresh the entity states."""
        self.pending[counter_id] = value
        self.async_update_listeners()

    def counter(self, counter_id: str) -> dict[str, Any]:
        for counter in (self.data or {}).get("counters") or []:
            if str(counter.get("id")) == counter_id:
                return counter
        return {}

    def assembly_counters(self, assembly_id: Any) -> list[str]:
        """Counter ids of one submission form (one «group» of meters)."""
        return [
            str(c.get("id"))
            for c in (self.data or {}).get("counters") or []
            if str(c.get("assembly_id")) == str(assembly_id)
        ]

    async def async_submit_readings(self, readings: dict[str, float]) -> None:
        """POST readings to the cabinet and refresh the data.

        ``readings``: ``{counter_id: value}`` — grouped by the meter's
        ``assembly_id`` (the site submits each assembly's form at once).
        """
        groups: dict[str, dict[str, float]] = {}
        for counter_id, value in readings.items():
            counter = self.counter(counter_id)
            meter_id = counter.get("meter_id")
            assembly_id = counter.get("assembly_id")
            if not meter_id or not assembly_id:
                raise HomeAssistantError(
                    f"Meter {counter_id} has no submission form data"
                )
            groups.setdefault(str(assembly_id), {})[str(meter_id)] = value
        for assembly_id, meters in groups.items():
            try:
                await self.client.async_submit_readings(assembly_id, meters)
            except SksAuthError as err:
                raise ConfigEntryAuthFailed(
                    f"SKS credentials rejected: {err}"
                ) from err
            except SksError as err:
                raise HomeAssistantError(str(err)) from err
        # Drop the pending slots that were just accepted.
        for counter_id, value in readings.items():
            if self.pending.get(counter_id) == value:
                self.pending.pop(counter_id, None)
        self.async_update_listeners()  # grey out the button right away
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.async_get_data()
        except SksAuthError as err:
            raise ConfigEntryAuthFailed(
                f"SKS credentials rejected: {err}"
            ) from err
        except SksError as err:
            raise UpdateFailed(str(err)) from err
        _LOGGER.debug(
            "Обновлено: ЛС %s, счётчиков %d, начислений %d",
            data.get("account_id"),
            len(data.get("counters") or []),
            len(data.get("accruals") or []),
        )
        return data


type SksConfigEntry = ConfigEntry[SksCoordinator]
