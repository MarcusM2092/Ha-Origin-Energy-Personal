"""Write Origin usage into Home Assistant long-term statistics.

Origin delivers dated hourly totals a day late, not a live meter, so this
writes *external statistics* (like core's `opower`) with a running sum instead
of faking an ever-increasing sensor. The Energy dashboard reads them directly.

Statistic ids:
    origin_energy_personal:<agreement>_energy_consumption   (kWh)
    origin_energy_personal:<agreement>_energy_cost          (AUD)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import OriginEnergyApiError, parse_api_datetime
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Units are declared by the API envelope; refuse unknown ones rather than
# silently mis-scaling a year of data.
ENERGY_TO_KWH = {"WATT_HOUR": 0.001, "KILOWATT_HOUR": 1.0}
COST_TO_DOLLARS = {"CENT": 0.01, "DOLLAR": 1.0}

# PREDICTED and MISSING are not real measurements.
USABLE_TYPES = {"ACTUAL", "APPROXIMATE", "PARTIAL"}

WRITE_BATCH = 720  # rows per recorder call (30 days of hours)


@dataclass(frozen=True)
class HourlyUsage:
    start: datetime  # tz-aware UTC, on the hour
    kwh: float
    dollars: float
    export_kwh: float = 0.0   # solar feed-in (0 if none)
    earnings: float = 0.0     # feed-in credit, dollars


def statistic_ids(agreement_id: str) -> tuple[str, str]:
    slug = re.sub(r"[^a-z0-9]+", "_", agreement_id.lower()).strip("_") or "account"
    return (
        f"{DOMAIN}:{slug}_energy_consumption",
        f"{DOMAIN}:{slug}_energy_cost",
    )


def feed_in_statistic_ids(agreement_id: str) -> tuple[str, str]:
    """Statistic ids for solar export (kWh) and feed-in earnings (AUD)."""
    base, _ = statistic_ids(agreement_id)
    prefix = base.removesuffix("_energy_consumption")
    return f"{prefix}_energy_export", f"{prefix}_feed_in_earnings"


def parse_points(
    points: list[dict[str, Any]],
    consumption_unit: str | None,
    cost_unit: str | None,
    tz: tzinfo,
) -> list[HourlyUsage]:
    """Turn raw API points into usable hourly rows (kWh and dollars)."""
    if not points:
        return []
    try:
        e_scale = ENERGY_TO_KWH[consumption_unit or ""]
        c_scale = COST_TO_DOLLARS[cost_unit or ""]
    except KeyError as err:
        raise OriginEnergyApiError(
            f"Unexpected unit from Origin: {consumption_unit!r}/{cost_unit!r}"
        ) from err

    rows: list[HourlyUsage] = []
    for point in points:
        if point.get("type") not in USABLE_TYPES:
            continue
        energy = point.get("totalConsumedEnergy")
        start = parse_api_datetime(point.get("startDate"), tz)
        if energy is None or start is None:
            continue
        start = start.astimezone(timezone.utc)
        if start.minute or start.second or start.microsecond:
            # HA statistics must sit on a UTC hour. (Half-hour-offset zones,
            # e.g. Adelaide, would need re-bucketing; not handled.)
            _LOGGER.warning("Skipping off-hour data point at %s", start)
            continue
        cost = point.get("totalCost")
        # ASSUMPTION (unverified upstream): feed-in uses the same declared units
        # as consumption and cost (Wh and cents). Check against a real export.
        feed = point.get("totalFeedInEnergy")
        earned = point.get("totalEarnings")
        rows.append(
            HourlyUsage(
                start=start,
                kwh=float(energy) * e_scale,
                dollars=float(cost) * c_scale if cost is not None else 0.0,
                export_kwh=float(feed) * e_scale if feed is not None else 0.0,
                earnings=float(earned) * c_scale if earned is not None else 0.0,
            )
        )
    return rows


async def async_get_last_start(hass: HomeAssistant, statistic_id: str) -> datetime | None:
    """Start of the newest stored statistic, or None if there are none."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = result.get(statistic_id) or []
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"])


async def async_get_base_sum(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float:
    """Running sum at the last stored hour before `before` (0 if none)."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        before - timedelta(days=7),
        before,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    series = result.get(statistic_id) or []
    if not series:
        return 0.0
    return float(series[-1].get("sum") or 0.0)


def _meta(statistic_id: str, name: str, unit: str, unit_class: str | None) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


async def async_write_statistics(
    hass: HomeAssistant,
    agreement_id: str,
    hours: list[HourlyUsage],
    window_start: datetime,
    include_feed_in: bool = False,
) -> None:
    """Write hours (sorted, de-duplicated) with sums continuing from before them.

    Re-importing is safe: statistics are keyed by start time, so a re-run
    overwrites rather than duplicates. This must only be used for a window
    that extends to the newest data, since later sums are derived from it.
    """
    if not hours:
        return
    energy_id, cost_id = statistic_ids(agreement_id)
    energy_sum = await async_get_base_sum(hass, energy_id, window_start)
    cost_sum = await async_get_base_sum(hass, cost_id, window_start)

    energy_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    for row in sorted(hours, key=lambda h: h.start):
        energy_sum += row.kwh
        cost_sum += row.dollars
        energy_stats.append(StatisticData(start=row.start, state=energy_sum, sum=energy_sum))
        cost_stats.append(StatisticData(start=row.start, state=cost_sum, sum=cost_sum))

    energy_meta = _meta(energy_id, "Origin Energy consumption", "kWh", "energy")
    cost_meta = _meta(cost_id, "Origin Energy cost", "AUD", None)

    for i in range(0, len(energy_stats), WRITE_BATCH):
        async_add_external_statistics(hass, energy_meta, energy_stats[i : i + WRITE_BATCH])
        async_add_external_statistics(hass, cost_meta, cost_stats[i : i + WRITE_BATCH])
    if include_feed_in:
        export_id, earn_id = feed_in_statistic_ids(agreement_id)
        export_sum = await async_get_base_sum(hass, export_id, window_start)
        earn_sum = await async_get_base_sum(hass, earn_id, window_start)
        export_stats: list[StatisticData] = []
        earn_stats: list[StatisticData] = []
        for row in sorted(hours, key=lambda h: h.start):
            export_sum += row.export_kwh
            earn_sum += row.earnings
            export_stats.append(StatisticData(start=row.start, state=export_sum, sum=export_sum))
            earn_stats.append(StatisticData(start=row.start, state=earn_sum, sum=earn_sum))
        export_meta = _meta(export_id, "Origin Energy export", "kWh", "energy")
        earn_meta = _meta(earn_id, "Origin Energy feed-in earnings", "AUD", None)
        for i in range(0, len(export_stats), WRITE_BATCH):
            async_add_external_statistics(hass, export_meta, export_stats[i : i + WRITE_BATCH])
            async_add_external_statistics(hass, earn_meta, earn_stats[i : i + WRITE_BATCH])

    _LOGGER.info(
        "Imported %d hours (%s -> %s)%s",
        len(energy_stats),
        energy_stats[0]["start"],
        energy_stats[-1]["start"],
        " incl. solar feed-in" if include_feed_in else "",
    )


# ---------------------------------------------------------------------------
# Gas (best effort; separate from the verified electricity path above)
# ---------------------------------------------------------------------------
# UNVERIFIED: the unit enum names Origin uses for gas. Names are normalised
# (upper-case, punctuation stripped) before lookup. An unrecognised unit is
# logged by the coordinator with its exact name instead of being guessed.
GAS_UNITS: dict[str, tuple[str, float, str]] = {
    # normalised name: (HA unit, scale to that unit, unit_class)
    "MEGAJOULE": ("MJ", 1.0, "energy"),
    "MJ": ("MJ", 1.0, "energy"),
    "GIGAJOULE": ("GJ", 1.0, "energy"),
    "GJ": ("GJ", 1.0, "energy"),
    "KILOWATTHOUR": ("kWh", 1.0, "energy"),
    "KWH": ("kWh", 1.0, "energy"),
    "WATTHOUR": ("kWh", 0.001, "energy"),
    "CUBICMETRE": ("m\u00b3", 1.0, "volume"),
    "CUBICMETER": ("m\u00b3", 1.0, "volume"),
    "M3": ("m\u00b3", 1.0, "volume"),
}


def resolve_gas_unit(unit: str | None) -> tuple[str, float, str] | None:
    """(HA unit, scale, unit_class) for Origin's declared gas unit, or None."""
    return GAS_UNITS.get(re.sub(r"[^A-Z0-9]", "", (unit or "").upper()))


def pick_gas_time_unit(available: list[str] | None) -> str:
    """HOURLY if the service advertises it, else DAILY.

    availableUsageTimeUnits under-reports (HALF_HOURLY works unlisted), so
    DAILY is tried even when not listed; an all-MISSING result is logged.
    """
    return "HOURLY" if "HOURLY" in (available or ()) else "DAILY"


@dataclass(frozen=True)
class GasUsage:
    start: datetime  # tz-aware UTC, on the hour
    value: float     # in the resolved HA unit
    dollars: float


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "account"


def gas_statistic_ids(agreement_id: str) -> tuple[str, str]:
    slug = _slug(agreement_id)
    return f"{DOMAIN}:{slug}_gas_consumption", f"{DOMAIN}:{slug}_gas_cost"


def parse_gas_points(
    points: list[dict[str, Any]],
    scale: float,
    cost_unit: str | None,
    tz: tzinfo,
) -> list[GasUsage]:
    """Usable gas points as rows (consumption scaled to the HA unit)."""
    if not points:
        return []
    try:
        c_scale = COST_TO_DOLLARS[cost_unit or ""]
    except KeyError as err:
        raise OriginEnergyApiError(f"Unexpected gas cost unit: {cost_unit!r}") from err

    rows: list[GasUsage] = []
    for point in points:
        if point.get("type") not in USABLE_TYPES:
            continue
        used = point.get("totalConsumedEnergy")
        start = parse_api_datetime(point.get("startDate"), tz)
        if used is None or start is None:
            continue
        start = start.astimezone(timezone.utc)
        if start.minute or start.second or start.microsecond:
            _LOGGER.warning("Skipping off-hour gas data point at %s", start)
            continue
        cost = point.get("totalCost")
        rows.append(
            GasUsage(
                start=start,
                value=float(used) * scale,
                dollars=float(cost) * c_scale if cost is not None else 0.0,
            )
        )
    return rows


async def async_write_gas_statistics(
    hass: HomeAssistant,
    agreement_id: str,
    rows: list[GasUsage],
    window_start: datetime,
    unit: str,
    unit_class: str,
) -> None:
    """Write gas consumption and cost with sums continuing from before the window."""
    if not rows:
        return
    use_id, cost_id = gas_statistic_ids(agreement_id)
    use_sum = await async_get_base_sum(hass, use_id, window_start)
    cost_sum = await async_get_base_sum(hass, cost_id, window_start)

    use_stats: list[StatisticData] = []
    cost_stats: list[StatisticData] = []
    for row in sorted(rows, key=lambda r: r.start):
        use_sum += row.value
        cost_sum += row.dollars
        use_stats.append(StatisticData(start=row.start, state=use_sum, sum=use_sum))
        cost_stats.append(StatisticData(start=row.start, state=cost_sum, sum=cost_sum))

    use_meta = _meta(use_id, "Origin Energy gas consumption", unit, unit_class)
    cost_meta = _meta(cost_id, "Origin Energy gas cost", "AUD", None)
    for i in range(0, len(use_stats), WRITE_BATCH):
        async_add_external_statistics(hass, use_meta, use_stats[i : i + WRITE_BATCH])
        async_add_external_statistics(hass, cost_meta, cost_stats[i : i + WRITE_BATCH])
    _LOGGER.info(
        "Imported %d gas rows (%s -> %s) in %s",
        len(use_stats), use_stats[0]["start"], use_stats[-1]["start"], unit,
    )
