"""The SKS (Российские коммунальные системы, lk.roscomsys.ru) integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SksClient
from .const import (
    CONF_ACCOUNT_ID,
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
)
from .coordinator import SksConfigEntry, SksCoordinator
from .statistics import async_import_history

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SksConfigEntry) -> bool:
    """Настройка интеграции SKS по записи конфигурации."""
    # Dedicated session — its cookie jar holds laravel_session; closed on
    # unload so a reload never leaks it.
    session = async_create_clientsession(hass, auto_cleanup=False)
    entry.async_on_unload(session.close)
    client = SksClient(
        session,
        login=entry.data[CONF_LOGIN],
        password=entry.data[CONF_PASSWORD],
        account_id=entry.data.get(CONF_ACCOUNT_ID),
    )

    coordinator = SksCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Backfill meter/accrual history into long-term statistics. Must finish
    # inside setup — if the recorder compiles a zero-point row (sum=0) before
    # the imported tail lands, the dashboard shows a huge negative delta seam.
    try:
        await async_import_history(hass, coordinator)
    except Exception:
        _LOGGER.exception("Не удалось импортировать историю счётчиков")
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SksConfigEntry) -> None:
    """Reload only when the scan interval actually changed."""
    coordinator = getattr(entry, "runtime_data", None)
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
    if coordinator is None or coordinator.update_interval != timedelta(
        minutes=minutes
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SksConfigEntry) -> bool:
    """Разгрузка интеграции."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
