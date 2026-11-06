"""Base entity for Juniper Junos."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import JunosCoordinator


def device_id(coordinator: JunosCoordinator) -> str:
    entry = coordinator.config_entry
    return entry.unique_id or entry.entry_id


def main_device_info(coordinator: JunosCoordinator) -> DeviceInfo:
    data = coordinator.data or {}
    sw = data.get("software") or {}
    inv = data.get("inventory") or {}
    return DeviceInfo(
        identifiers={(DOMAIN, device_id(coordinator))},
        manufacturer=MANUFACTURER,
        model=(sw.get("model") or inv.get("description") or "Junos device").upper(),
        name=sw.get("hostname") or coordinator.config_entry.title,
        sw_version=sw.get("version"),
        serial_number=inv.get("serial"),
    )


def member_device_info(coordinator: JunosCoordinator, member: str | None) -> DeviceInfo:
    """Virtual chassis members get their own device under the main one; standalone switches use the main device."""
    if not member or not coordinator.is_virtual_chassis:
        return main_device_info(coordinator)
    vc = {f"fpc{m['id']}": m for m in (coordinator.data or {}).get("virtual_chassis") or []}
    info = vc.get(member, {})
    return DeviceInfo(
        identifiers={(DOMAIN, f"{device_id(coordinator)}_{member}")},
        manufacturer=MANUFACTURER,
        model=(info.get("model") or "").upper() or None,
        name=f"{(coordinator.data.get('software') or {}).get('hostname') or coordinator.config_entry.title} {member}",
        serial_number=info.get("serial"),
        via_device=(DOMAIN, device_id(coordinator)),
    )


class JunosEntity(CoordinatorEntity[JunosCoordinator]):
    """An entity whose value is read from coordinator.data by a lookup function.

    ``lookup(data)`` returns the dict that holds this entity's values (e.g. one interface), or None when the
    thing has gone away (port removed, member down), which makes the entity unavailable.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: JunosCoordinator, key: str, name: str,
                 lookup: Callable[[dict[str, Any]], Any], device: DeviceInfo | None = None) -> None:
        super().__init__(coordinator)
        self._lookup = lookup
        self._attr_unique_id = f"{device_id(coordinator)}_{key}"
        self._attr_name = name
        self._attr_device_info = device or main_device_info(coordinator)

    @property
    def source(self) -> Any:
        try:
            return self._lookup(self.coordinator.data or {})
        except (KeyError, IndexError, TypeError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.source is not None


def find(items: list[dict[str, Any]] | None, field: str, value: Any) -> dict[str, Any] | None:
    for item in items or []:
        if item.get(field) == value:
            return item
    return None
