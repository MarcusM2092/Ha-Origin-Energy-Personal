"""Polling coordinator: refresh token, fetch usage, import statistics."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    OriginEnergyApiClient,
    OriginEnergyApiError,
    parse_api_datetime,
    pick_service,
)
from .auth import (
    OriginAuthConfigError,
    OriginAuthError,
    OriginAuthTransientError,
    async_refresh_access_token,
)
from .const import (
    CONF_AGREEMENT_ID,
    CONF_COOKIE,
    CONF_SCAN_HOURS,
    DEFAULT_SCAN_HOURS,
    DOMAIN,
    EXTRAS_REFRESH_HOURS,
    GAS_CHUNK_DAYS,
)
from .rates import normalize_rates
from .window import plan_chunks, plan_start
from .statistics import (
    GasUsage,
    HourlyUsage,
    async_get_last_start,
    async_write_gas_statistics,
    async_write_statistics,
    feed_in_statistic_ids,
    gas_statistic_ids,
    parse_gas_points,
    parse_points,
    pick_gas_time_unit,
    resolve_gas_unit,
    statistic_ids,
)

_LOGGER = logging.getLogger(__name__)


class OriginCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the session, the auth cookie and the statistics import."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.interval_hours: int = entry.options.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(hours=self.interval_hours),
        )
        # Own session with a real cookie jar: the auth cookie is seeded into
        # it each poll and the rotated value is read back out (see auth.py).
        # Not the shared HA session, which must not carry Origin cookies.
        self._session = async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar()
        )
        self._lock = asyncio.Lock()

        # Best-effort extras (gas + tariff rates). See _async_extras.
        self._extras_at: datetime | None = None
        self._gas_service: dict[str, Any] | None = None
        self._gas_unit: str | None = None
        self._gas_last_reading: datetime | None = None
        self._gas_logged = False
        self._rates: list[dict[str, Any]] = []

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            token = await self._async_token()
            client = OriginEnergyApiClient(self._session, token)
            try:
                return await self._async_sync(client)
            except OriginEnergyApiError as err:
                raise UpdateFailed(str(err)) from err

    async def _async_token(self) -> str:
        cookie = self.config_entry.data[CONF_COOKIE]
        try:
            result = await async_refresh_access_token(self._session, cookie)
        except OriginAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OriginAuthTransientError, OriginAuthConfigError) as err:
            raise UpdateFailed(str(err)) from err

        if result.cookie != cookie:
            # Auth0 slid the session expiry; keep the new cookie so the
            # session stays alive indefinitely. (The options-change listener
            # ignores this, so it does not trigger a reload.)
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_COOKIE: result.cookie},
            )
        return result.access_token

    async def _async_fetch(
        self,
        client: OriginEnergyApiClient,
        agreement_id: str,
        start: datetime,
        end: datetime,
        tz: tzinfo,
    ) -> tuple[dict[datetime, HourlyUsage], datetime | None, bool]:
        """Fetch [start, end] in chunks; returns hours, last reading, has_feed_in."""
        hours: dict[datetime, HourlyUsage] = {}  # de-dupes inclusive end dates
        last_reading: datetime | None = None
        has_feed_in = False
        for chunk_start, chunk_end in plan_chunks(start, end):
            window = await client.async_get_usage(agreement_id, chunk_start, chunk_end)
            has_feed_in = has_feed_in or window.has_feed_in
            for row in parse_points(
                window.points, window.consumption_unit, window.cost_unit, tz
            ):
                hours[row.start] = row
            if window.last_reading_date and (
                last_reading is None or window.last_reading_date > last_reading
            ):
                last_reading = window.last_reading_date
        return hours, last_reading, has_feed_in

    async def _async_sync(self, client: OriginEnergyApiClient) -> dict[str, Any]:
        agreement_id = self.config_entry.data[CONF_AGREEMENT_ID]

        services = await client.async_get_services()
        service = pick_service(services, agreement_id)
        if service is None or service.get("agreementId") != agreement_id:
            raise UpdateFailed(f"Agreement {agreement_id} not found on this account")

        tz_name = (
            (((service.get("property") or {}).get("address") or {}).get("components") or {})
            .get("timezone")
        )
        tz = (await dt_util.async_get_time_zone(tz_name)) if tz_name else None
        tz = tz or dt_util.get_default_time_zone()

        now = dt_util.utcnow()
        energy_id, _ = statistic_ids(agreement_id)
        last_start = await async_get_last_start(self.hass, energy_id)

        valid_from = parse_api_datetime(service.get("validFrom"), tz)
        if valid_from is not None:
            valid_from = valid_from.astimezone(timezone.utc)
        start = plan_start(now, last_start, valid_from)
        if last_start is None:
            _LOGGER.info("No statistics yet; backfilling from %s", start.date())

        hours, last_reading, has_feed_in = await self._async_fetch(
            client, agreement_id, start, now, tz
        )

        if has_feed_in:
            # Solar export stats are new to anyone who set this up before
            # feed-in support: if they don't exist yet, backfill them (and,
            # harmlessly and idempotently, the rest) from the agreement start.
            export_id, _ = feed_in_statistic_ids(agreement_id)
            if last_start is not None and await async_get_last_start(self.hass, export_id) is None:
                start = plan_start(now, None, valid_from)
                _LOGGER.info("Solar feed-in detected; backfilling export from %s", start.date())
                hours, last_reading, has_feed_in = await self._async_fetch(
                    client, agreement_id, start, now, tz
                )

        await async_write_statistics(
            self.hass, agreement_id, list(hours.values()), start, include_feed_in=has_feed_in
        )

        # Electricity is done and written. Everything below is best effort:
        # it is logged and swallowed, never allowed to fail the update.
        extras = await self._async_extras(client, service, tz, now)
        return {
            "last_update": dt_util.utcnow(),
            "last_reading": last_reading,
            **extras,
        }

    # ------------------------------------------------------------------
    # Best-effort extras: gas usage and tariff rates
    # ------------------------------------------------------------------
    async def _async_extras(
        self,
        client: OriginEnergyApiClient,
        electricity_service: dict[str, Any],
        tz: tzinfo,
        now: datetime,
    ) -> dict[str, Any]:
        if self._extras_at is None or now - self._extras_at >= timedelta(hours=EXTRAS_REFRESH_HOURS):
            self._extras_at = now
            try:
                await self._async_refresh_extras(client, electricity_service)
            except Exception:  # noqa: BLE001 - must never break electricity
                _LOGGER.warning("Gas/tariff lookup failed (electricity unaffected)", exc_info=True)

        if self._gas_service is not None:
            try:
                self._gas_last_reading = await self._async_sync_gas(client, tz, now)
            except Exception:  # noqa: BLE001 - must never break electricity
                _LOGGER.warning("Gas sync failed (electricity unaffected)", exc_info=True)

        return {
            "rates": self._rates,
            "gas_last_reading": self._gas_last_reading,
            "gas_unit": self._gas_unit,
        }

    async def _async_refresh_extras(
        self, client: OriginEnergyApiClient, electricity_service: dict[str, Any]
    ) -> None:
        """Find the live gas service and read current tariff rates."""
        try:
            gas = pick_service(await client.async_get_services("GAS", attempts=1))
        except OriginEnergyApiError as err:
            _LOGGER.warning("Could not look up a gas service: %s", err)
            gas = self._gas_service  # keep what we had
        if gas is not None and not gas.get("agreementId"):
            gas = None
        if gas is not None and self._gas_service is None:
            _LOGGER.info("Gas service found (agreement %s)", gas.get("agreementId"))
        self._gas_service = gas

        rates: list[dict[str, Any]] = []
        try:
            rates += normalize_rates(
                "electricity",
                await client.async_get_rates("ELECTRICITY", electricity_service["agreementId"]),
            )
        except OriginEnergyApiError as err:
            _LOGGER.warning("Could not read electricity tariff rates: %s", err)
        if gas is not None:
            try:
                rates += normalize_rates(
                    "gas", await client.async_get_rates("GAS", gas["agreementId"])
                )
            except OriginEnergyApiError as err:
                _LOGGER.warning("Could not read gas tariff rates: %s", err)
        if rates:  # keep the previous rates if a refresh came back empty
            self._rates = rates
            _LOGGER.info("Read %d tariff rates", len(rates))

    async def _async_sync_gas(
        self, client: OriginEnergyApiClient, tz: tzinfo, now: datetime
    ) -> datetime | None:
        """Import gas usage and cost; returns the last reading date."""
        gas = self._gas_service
        assert gas is not None
        agreement = gas["agreementId"]
        time_unit = pick_gas_time_unit(gas.get("availableUsageTimeUnits"))

        use_id, _ = gas_statistic_ids(agreement)
        last_start = await async_get_last_start(self.hass, use_id)
        valid_from = parse_api_datetime(gas.get("validFrom"), tz)
        if valid_from is not None:
            valid_from = valid_from.astimezone(timezone.utc)
        start = plan_start(now, last_start, valid_from)

        rows: dict[datetime, GasUsage] = {}
        last_reading: datetime | None = None
        resolved: tuple[str, float, str] | None = None
        raw_points = usable = 0

        for chunk_start, chunk_end in plan_chunks(start, now, GAS_CHUNK_DAYS[time_unit]):
            window = await client.async_get_usage(
                agreement, chunk_start, chunk_end, time_unit, "GAS", attempts=1
            )
            if window.last_reading_date and (
                last_reading is None or window.last_reading_date > last_reading
            ):
                last_reading = window.last_reading_date
            if not window.points:
                continue
            if resolved is None:
                resolved = resolve_gas_unit(window.consumption_unit)
                if resolved is None:
                    _LOGGER.warning(
                        "Unrecognised gas unit %r (cost unit %r): not importing gas. "
                        "Add it to GAS_UNITS in statistics.py.",
                        window.consumption_unit, window.cost_unit,
                    )
                    return last_reading
            new_rows = parse_gas_points(window.points, resolved[1], window.cost_unit, tz)
            raw_points += len(window.points)
            usable += len(new_rows)
            for row in new_rows:
                rows[row.start] = row

        if not self._gas_logged:
            self._gas_logged = True
            _LOGGER.info(
                "Gas diagnostics: granularity=%s advertised=%s unit=%s points=%d usable=%d lastReading=%s",
                time_unit, gas.get("availableUsageTimeUnits"),
                resolved[0] if resolved else None, raw_points, usable, last_reading,
            )

        if resolved is None or not rows:
            return last_reading
        self._gas_unit = resolved[0]
        await async_write_gas_statistics(
            self.hass, agreement, list(rows.values()), start, resolved[0], resolved[2]
        )
        return last_reading
