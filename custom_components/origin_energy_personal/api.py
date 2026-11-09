"""Thin GraphQL client for Origin Energy's Kraken-backed gateway.

Query shapes and gateway quirks follow the findings documented by
JKook-Plus/ha-origin-energy (MIT). Key traps handled here:

* `filter: null` crashes the backend -> the argument is never declared.
* Malformed documents return INTERNAL_SERVER_ERROR, not a parse error ->
  every query is checked for balanced braces at import time.
* Sub-daily ranges over 9 days fail -> callers chunk by HOURLY_CHUNK_DAYS.
* endDate is inclusive -> callers must de-duplicate on startDate.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any

import aiohttp

from .const import (
    BROWSER_HEADERS,
    GRAPHQL_URL,
    REQUEST_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

SERVICES_QUERY = """
query ServicesQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        ... on ElectricityService {
          agreementId
          active
          validFrom
          validTo
          availableUsageTimeUnits
          meterPoint {
            identifier
          }
          property {
            address {
              components {
                timezone
              }
            }
          }
        }
      }
    }
  }
}
"""

USAGE_QUERY = """
query UsageQuery($agreementId: String!, $type: ServiceType!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!) {
  viewer {
    kraken {
      service(type: $type, agreementId: $agreementId) {
        ... on ElectricityService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            hasFeedIn
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
              totalFeedInEnergy
              totalEarnings
            }
          }
        }
      }
    }
  }
}
"""

GAS_SERVICES_QUERY = """
query GasServicesQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        ... on GasService {
          agreementId
          active
          validFrom
          validTo
          availableUsageTimeUnits
          meterPoint {
            identifier
          }
        }
      }
    }
  }
}
"""

# Gas usage is a subset of the shape the original project queries for gas
# (no feed-in fields: there is no gas export).
GAS_USAGE_QUERY = """
query GasUsageQuery($agreementId: String!, $type: ServiceType!, $timeUnit: UsageTimeUnit!, $startDate: DateTime!, $endDate: DateTime!) {
  viewer {
    kraken {
      service(type: $type, agreementId: $agreementId) {
        ... on GasService {
          usage(timeUnit: $timeUnit, startDate: $startDate, endDate: $endDate, orderBy: START_DATE_ASC) {
            lastReadingDate
            unitsOfMeasure {
              totalConsumedEnergy
              totalCost
            }
            dataPoints {
              type
              startDate
              endDate
              totalConsumedEnergy
              totalCost
            }
          }
        }
      }
    }
  }
}
"""

# UNVERIFIED against a live account: written from the documented schema
# (product.rates with these fields). If it is wrong the gateway answers with
# an opaque error; the coordinator logs it and carries on without rates.
_RATES_FIELDS = """
          agreementId
          active
          product {
            rates {
              rateIncludingTax
              rateExcludingTax
              timeOfDay
              bandCategory
              networkTariffCode
              stepStart
              stepEnd
              activeFrom
              activeTo
            }
          }
"""
TARIFF_QUERY_ELECTRICITY = """
query TariffQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        ... on ElectricityService {%s        }
      }
    }
  }
}
""" % _RATES_FIELDS
TARIFF_QUERY_GAS = """
query TariffQuery($type: ServiceType!) {
  viewer {
    kraken {
      services(type: $type) {
        ... on GasService {%s        }
      }
    }
  }
}
""" % _RATES_FIELDS

for _q in (SERVICES_QUERY, USAGE_QUERY, GAS_SERVICES_QUERY, GAS_USAGE_QUERY,
           TARIFF_QUERY_ELECTRICITY, TARIFF_QUERY_GAS):
    # A typo here is indistinguishable from an Origin outage (see module doc).
    assert _q.count("{") == _q.count("}"), "unbalanced braces in GraphQL query"

# Not proof of a transient fault (the gateway also uses these for
# deterministic request errors) but cheap to retry on a once-a-few-hours poll.
RETRYABLE_ERROR_CODES = frozenset({"INTERNAL_SERVER_ERROR", "EXTERNAL_API_ERROR"})
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class OriginEnergyApiError(Exception):
    """Transport or GraphQL-level failure."""


class _RetryableError(OriginEnergyApiError):
    """A failure worth another attempt (private; retries are spent on escape)."""


@dataclass(frozen=True)
class UsageWindow:
    """One usage query's data points plus the envelope Origin sends with them."""

    points: list[dict[str, Any]]
    last_reading_date: datetime | None
    consumption_unit: str | None
    cost_unit: str | None
    has_feed_in: bool


def parse_api_datetime(value: str | None, default_tz: tzinfo | None = None) -> datetime | None:
    """Parse an Origin timestamp (tolerates trailing Z and date-only values)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None and default_tz is not None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def format_api_datetime(value: datetime) -> str:
    """Render an instant for the gateway's DateTime scalar.

    The literal Z means the components must BE UTC, so convert first.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def pick_service(
    services: list[dict[str, Any]], agreement_id: str | None = None
) -> dict[str, Any] | None:
    """Choose the live agreement.

    An account can carry a defunct agreement beside the live one; only the
    boolean `active` tells them apart (serviceState does not).
    """
    services = [s for s in services if s]  # union fragments can yield {}
    if not services:
        return None
    if agreement_id is not None:
        matched = next((s for s in services if s.get("agreementId") == agreement_id), None)
        if matched is not None:
            return matched
    pool = [s for s in services if s.get("active")] or services
    return max(pool, key=lambda s: s.get("validFrom") or "")


def _dig(data: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


class OriginEnergyApiClient:
    """Minimal GraphQL client for api.rx.originenergy.com.au."""

    def __init__(self, session: aiohttp.ClientSession, access_token: str) -> None:
        self._session = session
        # Per request, never as session defaults (HA overwrites those).
        self._headers = {**BROWSER_HEADERS, "Authorization": f"Bearer {access_token}"}

    async def _attempt(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=self._headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 500:
                    raise _RetryableError(f"GraphQL server error: {resp.status}")
                if resp.status != 200:
                    raise OriginEnergyApiError(f"GraphQL request failed: {resp.status}")
                payload = await resp.json()
        except OriginEnergyApiError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise _RetryableError(f"Error connecting to Origin Energy: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise OriginEnergyApiError(str(err)) from err

        if errors := payload.get("errors"):
            codes = {(e.get("extensions") or {}).get("code") for e in errors}
            if codes & RETRYABLE_ERROR_CODES:
                raise _RetryableError(str(errors))
            raise OriginEnergyApiError(str(errors))
        return payload["data"]

    async def _graphql(
        self, query: str, variables: dict[str, Any], attempts: int = REQUEST_ATTEMPTS
    ) -> dict[str, Any]:
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(query, variables)
            except _RetryableError as err:
                if attempt == attempts:
                    raise
                delay = RETRY_BACKOFF_SECONDS * attempt
                _LOGGER.debug(
                    "Origin request failed (attempt %d/%d), retrying in %ds: %s",
                    attempt, attempts, delay, err,
                )
                await asyncio.sleep(delay)
        raise OriginEnergyApiError("Request failed")  # unreachable

    async def async_get_services(
        self, service_type: str = "ELECTRICITY", *, attempts: int = REQUEST_ATTEMPTS
    ) -> list[dict[str, Any]]:
        query = GAS_SERVICES_QUERY if service_type == "GAS" else SERVICES_QUERY
        data = await self._graphql(query, {"type": service_type}, attempts)
        return _dig(data, "viewer", "kraken", "services", default=[])

    async def async_get_rates(
        self, service_type: str, agreement_id: str, *, attempts: int = 1
    ) -> list[dict[str, Any]]:
        """Current tariff rates for one agreement (best effort, no retries)."""
        query = TARIFF_QUERY_GAS if service_type == "GAS" else TARIFF_QUERY_ELECTRICITY
        data = await self._graphql(query, {"type": service_type}, attempts)
        services = [s for s in _dig(data, "viewer", "kraken", "services", default=[]) if s]
        service = pick_service(services, agreement_id)
        rates = _dig(service or {}, "product", "rates", default=[])
        return rates if isinstance(rates, list) else []

    async def async_get_usage(
        self,
        agreement_id: str,
        start_date: datetime,
        end_date: datetime,
        time_unit: str = "HOURLY",
        service_type: str = "ELECTRICITY",
        *,
        attempts: int = REQUEST_ATTEMPTS,
    ) -> UsageWindow:
        data = await self._graphql(
            GAS_USAGE_QUERY if service_type == "GAS" else USAGE_QUERY,
            {
                "agreementId": agreement_id,
                "type": service_type,
                "timeUnit": time_unit,
                "startDate": format_api_datetime(start_date),
                "endDate": format_api_datetime(end_date),
            },
            attempts,
        )
        usage = _dig(data, "viewer", "kraken", "service", "usage", default={})
        units = usage.get("unitsOfMeasure") or {}
        return UsageWindow(
            points=usage.get("dataPoints") or [],
            last_reading_date=parse_api_datetime(usage.get("lastReadingDate")),
            consumption_unit=units.get("totalConsumedEnergy"),
            cost_unit=units.get("totalCost"),
            has_feed_in=bool(usage.get("hasFeedIn")),
        )
