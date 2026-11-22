"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SksAuthError, SksClient, SksError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)

# A submitted value is shown in its reading box until the cabinet lists it
# (or a newer reading) — but no longer than this.
SUBMITTED_TIMEOUT = timedelta(hours=24)
# Tolerance for comparing readings as floats (readings step is 0.001).
_EPS = 1e-6


def _float(value: Any) -> float | None:
    """Convert stringy numbers ("1371", "1371,5") to float."""
    try:
        return float(str(value).replace(",", ".").replace(" ", "").strip())
    except (TypeError, ValueError):
        return None


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
        # Registry id of the account («ЛС») device — set in async_setup_entry,
        # meter devices link to it through via_device_id.
        self.account_device_id: str | None = None
        # Set once this run's statistics backfill has finished —
        # «Начислено итого» stays unavailable until then.
        self.history_ready = False
        # Values typed into the reading NumberEntities, waiting for the
        # submit button: {counter_id: value} (counter_id == serial).
        self.pending: dict[str, float] = {}
        # Values the cabinet accepted but does not list yet — the reading
        # boxes keep showing them instead of falling back to the previous
        # reading: {counter_id: value}.
        self.submitted: dict[str, float] = {}
        # {counter_id: (submission time, cabinet's last reading at that
        # moment)} — for the timeout and the «newer reading» check.
        self._submitted_meta: dict[str, tuple[datetime, float | None]] = {}

    def set_pending(self, counter_id: str, value: float) -> None:
        """Store a typed-in reading and refresh the entity states."""
        self.pending[counter_id] = value
        self.async_update_listeners()

    def counter(
        self, counter_id: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        for counter in (data if data is not None else self.data or {}).get(
            "counters"
        ) or []:
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
        # Move the accepted pending values to «submitted» — the boxes keep
        # the value until the cabinet lists it.
        now = dt_util.utcnow()
        for counter_id, value in readings.items():
            if counter_id in self.pending and self.pending[counter_id] == value:
                self.pending.pop(counter_id)
                self.submitted[counter_id] = value
                self._submitted_meta[counter_id] = (
                    now,
                    _float(self.counter(counter_id).get("reading")),
                )
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
        self._settle_submitted(data)
        return data

    def _settle_submitted(self, data: dict[str, Any]) -> None:
        """Forget submitted values once the cabinet's data catches up.

        A value is dropped when the meter's last reading in the cabinet
        equals it, when the cabinet shows a newer, larger reading than at
        submission time, or after SUBMITTED_TIMEOUT.
        """
        now = dt_util.utcnow()
        for counter_id, value in list(self.submitted.items()):
            sent_at, before = self._submitted_meta.get(counter_id, (now, None))
            last = _float(self.counter(counter_id, data).get("reading"))
            newer = last is not None and (
                before is None or abs(last - before) > _EPS
            )
            if (
                now - sent_at > SUBMITTED_TIMEOUT
                or (last is not None and abs(last - value) <= _EPS)
                or (newer and last > value)
            ):
                self.submitted.pop(counter_id, None)
                self._submitted_meta.pop(counter_id, None)


type SksConfigEntry = ConfigEntry[SksCoordinator]
