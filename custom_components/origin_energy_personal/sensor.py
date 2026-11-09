"""Sensors: diagnostics, plus tariff rates that appear when Origin reports them.

The energy data itself is NOT an entity: it lives in long-term statistics
(Developer tools -> Statistics) and is picked in the Energy dashboard.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OriginConfigEntry
from .const import CONF_AGREEMENT_ID, CONF_NMI, DOMAIN
from .coordinator import OriginCoordinator
from .rates import rate_unit

_DIAG = dict(device_class=SensorDeviceClass.TIMESTAMP, entity_category=EntityCategory.DIAGNOSTIC)

DESCRIPTIONS = (
    SensorEntityDescription(key="last_update", translation_key="last_update", **_DIAG),
    SensorEntityDescription(key="last_reading", translation_key="last_reading", **_DIAG),
)
GAS_DESCRIPTION = SensorEntityDescription(
    key="gas_last_reading", translation_key="gas_last_reading", **_DIAG
)


def _device(entry: OriginConfigEntry) -> DeviceInfo:
    agreement = entry.data[CONF_AGREEMENT_ID]
    return DeviceInfo(
        identifiers={(DOMAIN, agreement)},
        name="Origin Energy",
        manufacturer="Origin Energy",
        model=entry.data.get(CONF_NMI) or None,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OriginConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OriginSensor(coordinator, entry, d) for d in DESCRIPTIONS)

    known: set[str] = set()

    @callback
    def _add_new() -> None:
        """Add gas/rate sensors the first time the data shows they exist."""
        data = coordinator.data or {}
        new: list[SensorEntity] = []
        if data.get("gas_last_reading") and GAS_DESCRIPTION.key not in known:
            known.add(GAS_DESCRIPTION.key)
            new.append(OriginSensor(coordinator, entry, GAS_DESCRIPTION))
        for rate in data.get("rates") or []:
            if rate["key"] not in known:
                known.add(rate["key"])
                new.append(OriginRateSensor(coordinator, entry, rate["key"], rate["name"]))
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


class OriginSensor(CoordinatorEntity[OriginCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OriginCoordinator,
        entry: OriginConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_AGREEMENT_ID]}_{description.key}"
        self._attr_device_info = _device(entry)

    @property
    def native_value(self) -> datetime | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self.entity_description.key)


class OriginRateSensor(CoordinatorEntity[OriginCoordinator], SensorEntity):
    """One current tariff rate (e.g. electricity peak rate, supply charge)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OriginCoordinator,
        entry: OriginConfigEntry,
        rate_key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._rate_key = rate_key
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_AGREEMENT_ID]}_rate_{rate_key}"
        self._attr_device_info = _device(entry)

    def _rate(self) -> dict[str, Any] | None:
        for rate in (self.coordinator.data or {}).get("rates") or []:
            if rate["key"] == self._rate_key:
                return rate
        return None

    @property
    def available(self) -> bool:
        return super().available and self._rate() is not None

    @property
    def native_value(self) -> float | None:
        rate = self._rate()
        return rate["value"] if rate else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        rate = self._rate()
        if rate is None:
            return None
        return rate_unit(rate, (self.coordinator.data or {}).get("gas_unit"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rate = self._rate()
        if rate is None:
            return None
        return {
            "rate_excluding_tax": rate["value_ex_tax"],
            "network_tariff_code": rate["code"],
            "active_from": rate["active_from"],
            "active_to": rate["active_to"],
        }
