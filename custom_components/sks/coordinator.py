"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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
