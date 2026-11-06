"""Binary sensors for Juniper Junos: link state, alarms, fan / power supply / member health."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.parsers import merge_alarms
from .const import CONF_INTERFACES
from .coordinator import JunosConfigEntry, JunosCoordinator
from .entity import JunosEntity, find, member_device_info

PARALLEL_UPDATES = 0
OK_STATES = ("ok", "online", "prsnt", "present")


class JunosBinarySensor(JunosEntity, BinarySensorEntity):
    def __init__(self, coordinator: JunosCoordinator, key: str, name: str, lookup: Callable[[dict], Any],
                 is_on: Callable[[Any], bool | None], device_class: BinarySensorDeviceClass,
                 attrs: Callable[[Any], dict[str, Any]] | None = None, device=None,
                 category: EntityCategory | None = None, icon: str | None = None) -> None:
        super().__init__(coordinator, key, name, lookup, device)
        self._is_on = is_on
        self._attrs = attrs
        self._attr_device_class = device_class
        self._attr_entity_category = category
        if icon:
            self._attr_icon = icon

    @property
    def is_on(self) -> bool | None:
        src = self.source
        return None if src is None else self._is_on(src)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        src = self.source
        return None if src is None or self._attrs is None else self._attrs(src)


def _not_ok(s: dict[str, Any]) -> bool:
    return (s.get("status") or "").lower() not in OK_STATES


async def async_setup_entry(hass: HomeAssistant, entry: JunosConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    data = c.data or {}
    PROBLEM, CONN = BinarySensorDeviceClass.PROBLEM, BinarySensorDeviceClass.CONNECTIVITY
    ents: list[JunosBinarySensor] = []

    # ---- alarms ------------------------------------------------------------------------------------------------
    def alarms(d: dict[str, Any]) -> list[dict[str, Any]]:
        return merge_alarms(d.get("chassis_alarms"), d.get("system_alarms"))

    for cls in ("major", "minor"):
        ents.append(JunosBinarySensor(
            c, f"alarm_{cls}", f"{cls.capitalize()} alarm", lambda d: d,
            (lambda cc: lambda d: any(a["class"] == cc for a in alarms(d)))(cls), PROBLEM,
            attrs=(lambda cc: lambda d: {"alarms": [a["description"] for a in alarms(d) if a["class"] == cc]})(cls),
            icon="mdi:alarm-light-outline"))

    # ---- fans, power supplies (from chassis environment) ---------------------------------------------------------
    for item in data.get("environment") or []:
        if item["class"] not in ("fans", "power") or (item.get("status") or "").lower() == "absent":
            continue
        k = item["key"]
        ents.append(JunosBinarySensor(
            c, f"env_{k}_problem", f"{item['name']} status",
            (lambda kk: lambda d: find(d.get("environment"), "key", kk))(k), _not_ok, PROBLEM,
            attrs=lambda s: {"status": s["status"], "comment": s["comment"]},
            device=member_device_info(c, item.get("member")),
            icon="mdi:fan" if item["class"] == "fans" else "mdi:power-plug"))

    # ---- routing engines -------------------------------------------------------------------------------------------
    for re_ in data.get("route_engines") or []:
        if re_.get("status") is None:
            continue
        ents.append(JunosBinarySensor(
            c, f"{re_['key']}_problem", "Routing engine status",
            (lambda kk: lambda d: find(d.get("route_engines"), "key", kk))(re_["key"]), _not_ok, PROBLEM,
            attrs=lambda s: {"status": s["status"], "mastership": s["mastership"]},
            device=member_device_info(c, re_.get("member")), category=EntityCategory.DIAGNOSTIC))

    # ---- virtual chassis members --------------------------------------------------------------------------------------
    vc = data.get("virtual_chassis") or []
    if len(vc) > 1:
        for m in vc:
            ents.append(JunosBinarySensor(
                c, f"vc_{m['id']}_problem", f"Member {m['id']} status",
                (lambda mid: lambda d: find(d.get("virtual_chassis"), "id", mid))(m["id"]), _not_ok, PROBLEM,
                attrs=lambda s: {"status": s["status"], "role": s["role"]}))

    # ---- port link state ------------------------------------------------------------------------------------------------
    selected = entry.options.get(CONF_INTERFACES)
    for name in sorted(data.get("interfaces") or {}):
        if selected is not None and name not in selected:
            continue
        ents.append(JunosBinarySensor(
            c, f"if_{name}_link", f"{name} link",
            (lambda nn: lambda d: d["interfaces"].get(nn))(name), lambda s: s["oper_up"], CONN,
            attrs=lambda s: {"description": s["description"], "admin_up": s["admin_up"],
                             "speed": s["configured_speed"], "mac": s["mac"], "mtu": s["mtu"]},
            icon="mdi:ethernet"))

    async_add_entities(ents)
