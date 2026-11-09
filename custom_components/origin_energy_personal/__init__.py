"""Personal Origin Energy integration (read-only, electricity)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS
from .coordinator import OriginCoordinator

PLATFORMS = [Platform.SENSOR]

type OriginConfigEntry = ConfigEntry[OriginCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: OriginConfigEntry) -> bool:
    coordinator = OriginCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # The first run may be a multi-minute historical backfill (~1 request per
    # week of history), so run it in the background instead of blocking setup.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "origin_energy_personal_initial_sync"
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: OriginConfigEntry) -> None:
    """Reload only when the poll interval changed.

    The coordinator also writes a rotated auth cookie back into entry.data on
    every poll, which fires this listener too; reloading on that would loop.
    """
    if entry.options.get(CONF_SCAN_HOURS, DEFAULT_SCAN_HOURS) != entry.runtime_data.interval_hours:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OriginConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
