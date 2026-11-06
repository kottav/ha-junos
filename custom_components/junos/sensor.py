"""Sensors for Juniper Junos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.parsers import merge_alarms
from .const import CONF_INTERFACES, STORAGE_MOUNTS
from .coordinator import JunosConfigEntry, JunosCoordinator
from .entity import JunosEntity, find, member_device_info

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class JunosSensorDescription(SensorEntityDescription):
    """``value`` reads the state from the entity's source dict; ``attrs`` extra state attributes."""

    value: Callable[[Any], Any]
    attrs: Callable[[Any], dict[str, Any]] | None = None
    key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _ts(seconds: int | float | None) -> datetime | None:
    return None if not seconds else datetime.fromtimestamp(seconds, tz=timezone.utc)


TEMP = {"device_class": SensorDeviceClass.TEMPERATURE, "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
        "state_class": SensorStateClass.MEASUREMENT}
PCT = {"native_unit_of_measurement": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT,
       "suggested_display_precision": 0}
RATE = {"device_class": SensorDeviceClass.DATA_RATE, "native_unit_of_measurement": UnitOfDataRate.BITS_PER_SECOND,
        "suggested_unit_of_measurement": UnitOfDataRate.MEGABITS_PER_SECOND, "suggested_display_precision": 2,
        "state_class": SensorStateClass.MEASUREMENT}
BYTES = {"device_class": SensorDeviceClass.DATA_SIZE, "native_unit_of_measurement": UnitOfInformation.BYTES,
         "suggested_unit_of_measurement": UnitOfInformation.GIGABYTES, "suggested_display_precision": 2,
         "state_class": SensorStateClass.TOTAL_INCREASING}
COUNT = {"state_class": SensorStateClass.TOTAL_INCREASING}
POWER = {"device_class": SensorDeviceClass.POWER, "native_unit_of_measurement": UnitOfPower.WATT,
         "state_class": SensorStateClass.MEASUREMENT, "suggested_display_precision": 1}
DBM = {"device_class": SensorDeviceClass.SIGNAL_STRENGTH, "native_unit_of_measurement": SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
       "state_class": SensorStateClass.MEASUREMENT, "suggested_display_precision": 2}
DIAG = {"entity_category": EntityCategory.DIAGNOSTIC}
OFF = {"entity_registry_enabled_default": False}


class JunosSensor(JunosEntity, SensorEntity):
    entity_description: JunosSensorDescription

    def __init__(self, coordinator: JunosCoordinator, key: str, name: str, lookup: Callable[[dict], Any],
                 description: JunosSensorDescription, device=None) -> None:
        super().__init__(coordinator, key, name, lookup, device)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        src = self.source
        return None if src is None else self.entity_description.value(src)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        src = self.source
        if src is None or self.entity_description.attrs is None:
            return None
        return self.entity_description.attrs(src)


def _d(value: Callable[[Any], Any], *opts: dict[str, Any], attrs=None, icon: str | None = None) -> JunosSensorDescription:
    kw: dict[str, Any] = {}
    for o in opts:
        kw.update(o)
    return JunosSensorDescription(key="", value=value, attrs=attrs, icon=icon, **kw)


def _alarms(data: dict[str, Any]) -> list[dict[str, Any]]:
    return merge_alarms(data.get("chassis_alarms"), data.get("system_alarms"))


def _alarm_attrs(data: dict[str, Any]) -> dict[str, Any]:
    al = _alarms(data)
    return {
        "major": sum(1 for a in al if a["class"] == "major"),
        "minor": sum(1 for a in al if a["class"] == "minor"),
        "alarms": [f"{a['class'].capitalize()}: {a['description']}" for a in al],
    }


async def async_setup_entry(hass: HomeAssistant, entry: JunosConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    data = c.data or {}
    ents: list[JunosSensor] = []

    def add(key: str, name: str, lookup: Callable[[dict], Any], desc: JunosSensorDescription, device=None) -> None:
        ents.append(JunosSensor(c, key, name, lookup, desc, device))

    # ---- routing engine(s): CPU, memory, temperatures, load, boot time -------------------------------------
    for re_ in data.get("route_engines") or []:
        k = re_["key"]
        dev = member_device_info(c, re_.get("member"))
        lk = (lambda kk: lambda d: find(d.get("route_engines"), "key", kk))(k)
        add(f"{k}_cpu", "CPU utilization", lk, _d(lambda s: s["cpu_utilization"], PCT, icon="mdi:cpu-64-bit"), dev)
        add(f"{k}_memory", "Memory utilization", lk, _d(lambda s: s["memory_utilization"], PCT, icon="mdi:memory"), dev)
        if re_.get("temperature") is not None:
            add(f"{k}_temp", "Routing engine temperature", lk, _d(lambda s: s["temperature"], TEMP), dev)
        if re_.get("cpu_temperature") is not None:
            add(f"{k}_cpu_temp", "CPU temperature", lk, _d(lambda s: s["cpu_temperature"], TEMP), dev)
        for n, f, on in (("1 min", "load_1", True), ("5 min", "load_5", False), ("15 min", "load_15", False)):
            if re_.get(f) is not None:
                add(f"{k}_{f}", f"Load average ({n})", lk,
                    _d((lambda ff: lambda s: s[ff])(f), {"state_class": SensorStateClass.MEASUREMENT,
                                                         "suggested_display_precision": 2}, DIAG,
                       {} if on else OFF, icon="mdi:gauge"), dev)
        add(f"{k}_boot", "Last boot", lk, _d(
            lambda s: _ts(s["start_time"]) or _ts(round((datetime.now().timestamp() - (s["uptime_seconds"] or 0)) / 60) * 60)
            if (s["start_time"] or s["uptime_seconds"]) else None,
            {"device_class": SensorDeviceClass.TIMESTAMP}, DIAG,
            attrs=lambda s: {"reboot_reason": s.get("last_reboot_reason"), "mastership": s.get("mastership")}), dev)

    # ---- environment: temperatures, fans --------------------------------------------------------------------
    for item in data.get("environment") or []:
        if item.get("temperature") is None:
            continue
        k = item["key"]
        add(f"env_{k}", f"{item['name']} temperature",
            (lambda kk: lambda d: find(d.get("environment"), "key", kk))(k),
            _d(lambda s: s["temperature"], TEMP, attrs=lambda s: {"status": s["status"]}),
            member_device_info(c, item.get("member")))
    for fan in data.get("fans") or []:
        k = fan["key"]
        lk = (lambda kk: lambda d: find(d.get("fans"), "key", kk))(k)
        dev = member_device_info(c, fan.get("member"))
        if fan.get("rpm") is not None:
            add(f"fan_{k}_rpm", f"{fan['name']} speed", lk, _d(
                lambda s: s["rpm"], {"native_unit_of_measurement": REVOLUTIONS_PER_MINUTE,
                                     "state_class": SensorStateClass.MEASUREMENT},
                attrs=lambda s: {"status": s["status"], "comment": s["comment"]}, icon="mdi:fan"), dev)
        if fan.get("percent") is not None:
            add(f"fan_{k}_pct", f"{fan['name']} duty", lk, _d(lambda s: s["percent"], PCT, icon="mdi:fan"), dev)

    # ---- alarms -----------------------------------------------------------------------------------------------
    add("alarms", "Active alarms", lambda d: d, _d(
        lambda d: len(_alarms(d)), {"state_class": SensorStateClass.MEASUREMENT}, attrs=_alarm_attrs,
        icon="mdi:alarm-light"))

    # ---- interfaces ------------------------------------------------------------------------------------------
    selected = entry.options.get(CONF_INTERFACES)
    for name, iface in sorted((data.get("interfaces") or {}).items()):
        if selected is not None and name not in selected:
            continue
        lk = (lambda nn: lambda d: d["interfaces"].get(nn))(name)
        attrs = lambda s: {"description": s["description"]}  # noqa: E731
        specs = [
            ("rx_rate", "RX throughput", _d(lambda s: s["rx_rate"], RATE, attrs=attrs, icon="mdi:download-network")),
            ("tx_rate", "TX throughput", _d(lambda s: s["tx_rate"], RATE, attrs=attrs, icon="mdi:upload-network")),
            ("rx_bytes", "RX total", _d(lambda s: s["rx_bytes"], BYTES, OFF)),
            ("tx_bytes", "TX total", _d(lambda s: s["tx_bytes"], BYTES, OFF)),
            ("rx_pkt_rate", "RX packet rate", _d(lambda s: s["rx_pkt_rate"], {
                "native_unit_of_measurement": "p/s", "state_class": SensorStateClass.MEASUREMENT}, OFF)),
            ("tx_pkt_rate", "TX packet rate", _d(lambda s: s["tx_pkt_rate"], {
                "native_unit_of_measurement": "p/s", "state_class": SensorStateClass.MEASUREMENT}, OFF)),
            ("rx_errors", "RX errors", _d(lambda s: s["rx_errors"], COUNT, DIAG, icon="mdi:alert-circle-outline")),
            ("tx_errors", "TX errors", _d(lambda s: s["tx_errors"], COUNT, DIAG, icon="mdi:alert-circle-outline")),
            ("rx_drops", "RX drops", _d(lambda s: s["rx_drops"], COUNT, DIAG, OFF)),
            ("tx_drops", "TX drops", _d(lambda s: s["tx_drops"], COUNT, DIAG, OFF)),
            ("carrier_transitions", "Link flaps", _d(lambda s: s["carrier_transitions"], COUNT, DIAG, OFF,
                                                      icon="mdi:swap-vertical")),
            ("crc_errors", "CRC errors", _d(lambda s: s["crc_errors"], COUNT, DIAG, OFF)),
            ("speed", "Link speed", _d(lambda s: s["speed_mbps"] if s["oper_up"] else None, {
                "device_class": SensorDeviceClass.DATA_RATE,
                "native_unit_of_measurement": UnitOfDataRate.MEGABITS_PER_SECOND}, DIAG, OFF)),
            ("flapped", "Last link change", _d(lambda s: _ts(s["last_flapped"]),
                                               {"device_class": SensorDeviceClass.TIMESTAMP}, DIAG, OFF)),
        ]
        for key, label, desc in specs:
            if iface.get(key.replace("_rate", "_bytes") if key in ("rx_rate", "tx_rate") else key) is None \
                    and key not in ("speed", "flapped", "rx_rate", "tx_rate", "rx_pkt_rate", "tx_pkt_rate"):
                continue
            add(f"if_{name}_{key}", f"{name} {label}", lk, desc)

    # ---- optics ------------------------------------------------------------------------------------------------
    for name in sorted(data.get("optics") or {}):
        if selected is not None and name not in selected:
            continue
        lk = (lambda nn: lambda d: (d.get("optics") or {}).get(nn))(name)
        add(f"optic_{name}_rx", f"{name} optic RX power", lk, _d(lambda s: s["rx_power_dbm"], DBM))
        add(f"optic_{name}_tx", f"{name} optic TX power", lk, _d(lambda s: s["tx_power_dbm"], DBM))
        add(f"optic_{name}_temp", f"{name} optic temperature", lk, _d(lambda s: s["temperature"], TEMP))

    # ---- PoE ---------------------------------------------------------------------------------------------------
    if data.get("poe_controller"):
        add("poe_power", "PoE power", lambda d: d.get("poe_controller") or None, _d(
            lambda s: round(sum(x["power_w"] or 0 for x in s), 1), POWER, icon="mdi:flash"))
        add("poe_budget", "PoE budget", lambda d: d.get("poe_controller") or None, _d(
            lambda s: round(sum(x["power_limit_w"] or 0 for x in s), 1), POWER, DIAG, icon="mdi:flash-outline"))
    for name in sorted(data.get("poe_interfaces") or {}):
        if selected is not None and name not in selected:
            continue
        add(f"poe_{name}", f"{name} PoE power", (lambda nn: lambda d: (d.get("poe_interfaces") or {}).get(nn))(name),
            _d(lambda s: s["power_w"], POWER,
               attrs=lambda s: {"status": s["status"], "class": s["class"], "priority": s["priority"],
                                "max_power_w": s["max_power_w"], "enabled": s["enabled"]}, icon="mdi:ethernet"))

    # ---- tables ------------------------------------------------------------------------------------------------
    if "mac_count" in data:
        add("mac_count", "MAC table entries", lambda d: d if "mac_count" in d else None,
            _d(lambda d: d["mac_count"], {"state_class": SensorStateClass.MEASUREMENT}, icon="mdi:table-network"))
    if "arp_count" in data:
        add("arp_count", "ARP entries", lambda d: d if "arp_count" in d else None,
            _d(lambda d: d["arp_count"], {"state_class": SensorStateClass.MEASUREMENT}, DIAG, icon="mdi:table"))
    if "lldp" in data:
        add("lldp", "LLDP neighbors", lambda d: d.get("lldp") if "lldp" in d else None, _d(
            len, {"state_class": SensorStateClass.MEASUREMENT}, icon="mdi:lan-connect",
            attrs=lambda s: {"neighbors": [f"{n['local_port']}: {n['system'] or n['chassis_id']} ({n['port']})"
                                           for n in s]}))

    # ---- storage -----------------------------------------------------------------------------------------------
    for fs in data.get("storage") or []:
        if fs.get("mount") not in STORAGE_MOUNTS or fs.get("used_percent") is None:
            continue
        key = f"{fs.get('member') or ''}{fs['mount']}"
        label = fs["mount"].removeprefix("/.mount").strip("/") or "root"
        add(f"storage_{key}", f"Storage used ({label})",
            (lambda m, mt: lambda d: next((x for x in d.get("storage") or []
                                           if x.get("member") == m and x["mount"] == mt), None))(fs.get("member"), fs["mount"]),
            _d(lambda s: s["used_percent"], PCT, DIAG, icon="mdi:harddisk",
               attrs=lambda s: {"device": s["device"]}), member_device_info(c, fs.get("member")))

    # ---- virtual chassis ---------------------------------------------------------------------------------------
    vc = data.get("virtual_chassis") or []
    if len(vc) > 1:
        for m in vc:
            add(f"vc_{m['id']}_role", f"Member {m['id']} role",
                (lambda mid: lambda d: find(d.get("virtual_chassis"), "id", mid))(m["id"]),
                _d(lambda s: s["role"], DIAG, icon="mdi:server-network",
                   attrs=lambda s: {"status": s["status"], "model": s["model"], "serial": s["serial"],
                                    "priority": s["priority"]}))

    # ---- config ------------------------------------------------------------------------------------------------
    if data.get("commit"):
        add("last_commit", "Last configuration change", lambda d: d.get("commit"), _d(
            lambda s: _ts(s["time"]), {"device_class": SensorDeviceClass.TIMESTAMP}, DIAG, icon="mdi:file-cog",
            attrs=lambda s: {"user": s["user"], "client": s["client"], "comment": s["comment"]}))

    async_add_entities(ents)
