"""Backfill cabinet history into the sensors' long-term statistics.

The «Показания» counter sensor is TOTAL_INCREASING — its statistics feed the
History panel and the water section of the energy dashboard. Two sources are
used, in order:

1. the readings history table on the counters page (dated meter readings —
   imported verbatim);
2. monthly volumes from the accruals page — only when the account has a
   single meter, since a volume row can't be attributed to a specific meter
   otherwise. Cumulative sums are reconstructed so each month lands as its
   own bar.

Monthly charged (₽) and volume (m³) rows are also imported into the
«Начислено» / «Расход за месяц» sensors — as cumulative ``sum`` the
«Расход за месяц» sensor works on the water dashboard even without meter
history.

Only rows older than the oldest recorded statistic are imported — restarts
never duplicate data, and readings that drop below the previous accepted
value (meter replacement artefacts) are skipped to keep the series
monotonic.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMetaData,
    get_last_statistics,
    import_statistics,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import SksCoordinator
from .sensor import _float, _naive, _parse_dt

_LOGGER = logging.getLogger(__name__)

RUB = "₽"
_UNIT_CLASS = {"m³": "volume", "kWh": "energy", "Wh": "energy", "₽": None}


def _hour(dt: datetime | None) -> datetime | None:
    return dt.replace(minute=0, second=0, microsecond=0) if dt else None


def _reading_stats(counter: dict[str, Any]) -> list[StatisticData]:
    """Monotonic rows from the counter's dated readings."""
    stats: list[StatisticData] = []
    prev: float | None = None
    rows = sorted(
        (h for h in counter.get("history") or [] if isinstance(h, dict)),
        key=lambda h: _naive(h.get("date")),
    )
    for row in rows:
        start = _hour(_parse_dt(row.get("date")))
        reading = _float(row.get("reading"))
        if start is None or reading is None:
            continue
        if prev is not None and reading <= prev:
            continue
        prev = reading
        stats = [s for s in stats if s["start"] != start]
        stats.append(StatisticData(start=start, state=reading, sum=reading))
    return stats


def _accrual_rows(coordinator: SksCoordinator) -> list[dict[str, Any]]:
    rows = [
        a
        for a in (coordinator.data or {}).get("accruals") or []
        if isinstance(a, dict)
    ]
    rows.sort(key=lambda a: _naive(a.get("_dt") or a.get("date")))
    return rows


def _cumulative_stats(
    rows: list[dict[str, Any]], field: str
) -> list[StatisticData]:
    """Cumulative ``sum`` built from monthly ``field`` deltas."""
    stats: list[StatisticData] = []
    total = 0.0
    for row in rows:
        start = _hour(_parse_dt(row.get("_dt") or row.get("date")))
        value = _float(row.get(field))
        if start is None or value is None:
            continue
        total += value
        stats = [s for s in stats if s["start"] != start]
        stats.append(StatisticData(start=start, state=value, sum=round(total, 2)))
    return stats


def _stat_start(row: dict[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, datetime):
        return start
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return _parse_dt(start)


def _metadata(hass: HomeAssistant, entity_id: str, unit: str, has_mean: bool, has_sum: bool) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    return {
        "has_mean": has_mean,
        "has_sum": has_sum,
        "name": state.name if state else None,
        "source": "recorder",
        "statistic_id": entity_id,
        "unit_of_measurement": unit,
    }


async def _import_stats(
    hass: HomeAssistant,
    entity_id: str,
    metadata: dict[str, Any],
    stats: list[StatisticData],
    stat_types: set[str],
) -> None:
    """Import stats rows older than the entity's oldest recorded statistic."""
    if not stats:
        return
    existing = await hass.async_add_executor_job(
        get_last_statistics, hass, 5000, entity_id, True, stat_types
    )
    rows = existing.get(entity_id) or []
    starts = [s for s in (_stat_start(r) for r in rows) if s]
    if (oldest := min(starts, default=None)) is not None:
        stats = [s for s in stats if s["start"] < oldest]
    if not stats:
        return
    # A stale row after the imported tail with a lower sum (e.g. a zero-point
    # left behind by a purge) becomes a huge negative delta in the dashboard.
    # The import can't fix existing rows — warn so the entity can be purged.
    last_sum = stats[-1].get("sum")
    if last_sum:
        bad = [
            r
            for r in rows
            if (s := _stat_start(r))
            and s > stats[-1]["start"]
            and (r.get("sum") or 0.0) < last_sum
        ]
        if bad:
            _LOGGER.warning(
                "%s: после импортированной истории (sum=%s) есть строки с "
                "меньшим sum — на дашборде будет отрицательный скачок. "
                "Очистите статистику сущности и перезапустите HA.",
                entity_id,
                last_sum,
            )
    # Current HA API: statistics.async_import_statistics(hass, metadata,
    # stats) — a @callback that validates input and queues the job on the
    # recorder thread itself. Older versions had the sync
    # import_statistics(hass, metadata, stats) — kept as fallback.
    try:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import (
            async_import_statistics,
        )

        new_meta = dict(metadata)
        new_meta.setdefault(
            "mean_type",
            StatisticMeanType.ARITHMETIC
            if new_meta.get("has_mean")
            else StatisticMeanType.NONE,
        )
        new_meta.setdefault(
            "unit_class", _UNIT_CLASS.get(new_meta.get("unit_of_measurement"))
        )
        async_import_statistics(hass, new_meta, stats)
        if new_meta.get("has_sum") and stats:
            _seed_short_term(hass, new_meta, stats[-1])
    except (ImportError, TypeError):
        await hass.async_add_executor_job(
            import_statistics, hass, StatisticMetaData(**metadata), stats
        )
    _LOGGER.debug("Импортировано %d записей истории в %s", len(stats), entity_id)


def _seed_short_term(
    hass: HomeAssistant, metadata: dict[str, Any], tail: StatisticData
) -> None:
    """Mirror the imported tail into ``statistics_short_term``.

    The 5-minute statistics compiler continues ``sum`` from the latest
    *short-term* row — not from ``statistics``. Without this seed it finds an
    empty table, writes a zero-point row (sum=0), and the hourly rollup turns
    it into a giant negative delta right after the imported history.
    """
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.db_schema import (
            StatisticsShortTerm,
        )
        from homeassistant.components.recorder.tasks import ImportStatisticsTask

        if (instance := get_instance(hass)) is None:
            return
        seed = StatisticData(
            start=dt_util.utcnow(),
            state=tail.get("state"),
            sum=tail.get("sum"),
        )
        instance.queue_task(
            ImportStatisticsTask(
                StatisticMetaData(**metadata), [seed], StatisticsShortTerm
            )
        )
    except Exception:  # best effort — seeding must never break setup
        _LOGGER.debug(
            "Не удалось сидировать short-term статистику", exc_info=True
        )


async def _drain_recorder(hass: HomeAssistant) -> None:
    """Wait briefly for the recorder to commit queued statistic inserts.

    ``async_import_statistics`` only enqueues the write — without the drain a
    statistics compile can run first, see "no stats" and write a zero-point
    row, which later renders as a giant negative delta in the dashboard.
    The wait is bounded: a stuck recorder queue must never hang entry setup.
    """
    try:
        from homeassistant.components.recorder import get_instance

        if (instance := get_instance(hass)) is None:
            return
        await asyncio.wait_for(instance.async_block_till_done(), timeout=60)
    except (asyncio.TimeoutError, TimeoutError):
        _LOGGER.warning("Очередь recorder не освободилась за 60с — продолжаем")
    except Exception:  # best effort — recorder internals vary by version
        _LOGGER.debug("Не удалось дождаться очереди recorder", exc_info=True)


async def async_import_history(
    hass: HomeAssistant, coordinator: SksCoordinator
) -> None:
    """Import cabinet history into sensor statistics, once."""
    registry = er.async_get(hass)
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or "")
    accruals = _accrual_rows(coordinator)

    # --- counter readings -------------------------------------------------
    counters = [c for c in data.get("counters") or [] if isinstance(c, dict)]
    for counter in counters:
        counter_id = str(counter.get("id") or counter.get("serial") or "meter")
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{account_id}_{counter_id}_reading"
        )
        if entity_id:
            try:
                await _import_stats(
                    hass,
                    entity_id,
                    _metadata(
                        hass, entity_id, UnitOfVolume.CUBIC_METERS, False, True
                    ),
                    _reading_stats(counter),
                    {"sum"},
                )
            except Exception:
                _LOGGER.exception(
                    "Не удалось импортировать историю %s", entity_id
                )

    # --- monthly charged (₽) ----------------------------------------------
    charged_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{account_id}_charged"
    )
    if charged_id:
        try:
            await _import_stats(
                hass,
                charged_id,
                _metadata(hass, charged_id, RUB, False, True),
                _cumulative_stats(accruals, "charged"),
                {"sum"},
            )
        except Exception:
            _LOGGER.exception("Не удалось импортировать историю %s", charged_id)

    # --- monthly consumption (m³) ------------------------------------------
    # Monthly volumes live on the counters page (Расход column); unambiguous
    # only with a single meter.
    consumption_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{account_id}_consumption"
    )
    if consumption_id and len(counters) == 1:
        volume_rows = [
            {"_dt": h.get("date") or h.get("period_dt"), "volume": h.get("volume")}
            for h in counters[0].get("history") or []
            if isinstance(h, dict)
        ]
        volume_rows.sort(key=lambda r: _naive(r.get("_dt")))
        try:
            await _import_stats(
                hass,
                consumption_id,
                _metadata(
                    hass,
                    consumption_id,
                    UnitOfVolume.CUBIC_METERS,
                    False,
                    True,
                ),
                _cumulative_stats(volume_rows, "volume"),
                {"sum"},
            )
        except Exception:
            _LOGGER.exception(
                "Не удалось импортировать историю %s", consumption_id
            )
    await _drain_recorder(hass)
