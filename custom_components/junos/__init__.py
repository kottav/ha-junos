"""Juniper Junos switches in Home Assistant (NETCONF over SSH)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_INTERFACES
from .coordinator import JunosConfigEntry, JunosCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: JunosConfigEntry) -> bool:
    coordinator = JunosCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.client.close()
        raise
    entry.runtime_data = coordinator
    _remove_deselected_port_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_update))
    return True


def _remove_deselected_port_entities(hass: HomeAssistant, entry: JunosConfigEntry) -> None:
    """Ports unticked in the options would otherwise linger as 'unavailable' entities."""
    selected = entry.options.get(CONF_INTERFACES)
    if selected is None:
        return
    keep = set(selected)
    base = entry.unique_id or entry.entry_id
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        uid = ent.unique_id
        for kind in ("if_", "optic_", "poe_"):
            prefix = f"{base}_{kind}"
            if uid.startswith(prefix):
                port = uid[len(prefix):].split("_", 1)[0]  # port names never contain "_"
                if port not in keep:
                    reg.async_remove(ent.entity_id)
                break


async def async_unload_entry(hass: HomeAssistant, entry: JunosConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.client.close()
    return ok


async def _reload_on_update(hass: HomeAssistant, entry: JunosConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
