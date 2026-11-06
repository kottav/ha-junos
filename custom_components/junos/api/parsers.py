"""Turn namespace-stripped Junos RPC replies into plain dicts.

Every parser is tolerant: missing elements give ``None`` rather than exceptions, because Junos output
differs between platforms (EX2300 vs EX4300 vs QFX vs SRX), releases and virtual-chassis setups.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Any

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
PHYSICAL_PORT = re.compile(r"^(?:(ge|xe|et|mge|fe|xle|fte|ce)-(\d+)/(\d+)/(\d+)(?::(\d+))?|ae(\d+))$")
"""Front-panel ports (incl. channelised ``xe-0/0/0:2``) and aggregates (``ae0``). Not internal/logical ifds."""


def _t(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    if "inf" in v.lower():
        return None
    m = _NUM.search(v.replace(",", ""))
    return float(m.group()) if m else None


def _int(v: str | None) -> int | None:
    n = _num(v)
    return None if n is None else int(n)


def _attr(el: ET.Element | None, path: str, attr: str) -> str | None:
    if el is None:
        return None
    x = el.find(path)
    if x is None:
        return None
    return x.attrib.get(attr)


def _celsius(el: ET.Element | None, path: str) -> float | None:
    """<temperature junos:celsius="45">45 degrees C / 113 degrees F</temperature>"""
    c = _attr(el, path, "celsius")
    if c is not None:
        return _num(c)
    return _num(_t(el, path))


def _seconds(el: ET.Element | None, path: str) -> int | None:
    s = _attr(el, path, "seconds")
    return _int(s) if s is not None else None


def multi_re(root: ET.Element, tag: str) -> list[tuple[str | None, ET.Element]]:
    """Return [(re-name or None, element)] for single and multi-routing-engine replies."""
    items = root.findall(".//multi-routing-engine-item")
    if items:
        out = []
        for item in items:
            name = _t(item, "re-name")
            for el in item.iter(tag):
                out.append((name, el))
        return out
    return [(None, el) for el in root.iter(tag)]


# --------------------------------------------------------------------------------------------- system

def parse_software(root: ET.Element) -> dict[str, Any]:
    info = next(iter(root.iter("software-information")), None)
    if info is None:
        return {}
    version = _t(info, "junos-version")
    if version is None:  # old releases: <package-information><comment>JUNOS Base OS boot [12.3R12.4]</comment>
        for c in info.iter("comment"):
            m = re.search(r"\[([^\]]+)\]", c.text or "")
            if m:
                version = m.group(1)
                break
    return {
        "hostname": _t(info, "host-name"),
        "model": _t(info, "product-model"),
        "name": _t(info, "product-name"),
        "version": version,
    }


def parse_inventory(root: ET.Element) -> dict[str, Any]:
    chassis = next(iter(root.iter("chassis")), None)
    out = {"serial": _t(chassis, "serial-number"), "description": _t(chassis, "description"), "modules": []}
    if chassis is not None:
        for mod in chassis.findall("chassis-module"):
            out["modules"].append({
                "name": _t(mod, "name"), "serial": _t(mod, "serial-number"),
                "description": _t(mod, "description"), "model": _t(mod, "model-number"),
            })
    return out


def parse_route_engines(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for member, re_el in multi_re(root, "route-engine"):
        idle = _num(_t(re_el, "cpu-idle"))
        if idle is None:  # some releases only have the 1/5/15 min split
            idle = _num(_t(re_el, "cpu-idle1"))
        slot = _t(re_el, "slot")
        out.append({
            "member": member,
            "slot": slot,
            "key": member or (f"re{slot}" if slot is not None else "re0"),
            "mastership": _t(re_el, "mastership-state"),
            "status": _t(re_el, "status"),
            "model": _t(re_el, "model"),
            "serial": _t(re_el, "serial-number"),
            "temperature": _celsius(re_el, "temperature"),
            "cpu_temperature": _celsius(re_el, "cpu-temperature"),
            "memory_total_mb": _num(_t(re_el, "memory-dram-size") or _t(re_el, "memory-installed-size")),
            "memory_utilization": _num(_t(re_el, "memory-buffer-utilization")),
            "cpu_user": _num(_t(re_el, "cpu-user")),
            "cpu_system": _num(_t(re_el, "cpu-system")),
            "cpu_interrupt": _num(_t(re_el, "cpu-interrupt")),
            "cpu_idle": idle,
            "cpu_utilization": None if idle is None else round(100.0 - idle, 1),
            "uptime_seconds": _seconds(re_el, "up-time"),
            "start_time": _seconds(re_el, "start-time"),
            "load_1": _num(_t(re_el, "load-average-one")),
            "load_5": _num(_t(re_el, "load-average-five")),
            "load_15": _num(_t(re_el, "load-average-fifteen")),
            "last_reboot_reason": _t(re_el, "last-reboot-reason"),
        })
    return out


# ---------------------------------------------------------------------------------------- environment

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_environment(root: ET.Element) -> list[dict[str, Any]]:
    """show chassis environment. <class> is only on the first item of each group, so it is carried forward."""
    out = []
    groups = multi_re(root, "environment-information") or [(None, root)]
    for member, env in groups:
        cls = None
        for item in env.findall("environment-item"):
            cls = _t(item, "class") or cls
            name = _t(item, "name") or "?"
            out.append({
                "member": member,
                "key": _slug(f"{member or ''} {name}"),
                "name": name,
                "class": (cls or "").lower(),
                "status": _t(item, "status"),
                "temperature": _celsius(item, "temperature"),
                "comment": _t(item, "comment"),
            })
    return out


def parse_fans(root: ET.Element) -> list[dict[str, Any]]:
    """show chassis fan (EX/QFX); RPM where the platform reports it."""
    out = []
    for member, item in multi_re(root, "fan-information-rpm-item"):
        name = _t(item, "name") or "?"
        out.append({
            "member": member,
            "key": _slug(f"{member or ''} {name}"),
            "name": name,
            "status": _t(item, "status"),
            "rpm": _int(_t(item, "rpm")),
            "percent": _int(_t(item, "speed-percentage") or _t(item, "fan-speed-percentage")),
            "comment": _t(item, "comment"),
        })
    return out


# --------------------------------------------------------------------------------------------- alarms

def parse_alarms(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for d in root.iter("alarm-detail"):
        out.append({
            "class": (_t(d, "alarm-class") or "").lower(),
            "description": _t(d, "alarm-description"),
            "short": _t(d, "alarm-short-description"),
            "type": _t(d, "alarm-type"),
            "time": _seconds(d, "alarm-time"),
        })
    return out


def merge_alarms(*lists: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Chassis + system alarms without duplicates (system alarms repeat chassis alarms on many releases)."""
    seen, out = set(), []
    for lst in lists:
        for a in lst or []:
            k = (a["class"], a["description"], a["time"])
            if k not in seen:
                seen.add(k)
                out.append(a)
    return out


# ---------------------------------------------------------------------------------------- interfaces

def _speed_mbps(v: str | None) -> float | None:
    if not v:
        return None
    s = v.lower()
    n = _num(s)
    if n is None:
        return None
    if "gbps" in s or s.endswith("g"):
        return n * 1000
    if "kbps" in s:
        return n / 1000
    return n  # mbps


def _flapped(phy: ET.Element) -> int | None:
    ago = _seconds(phy, "interface-flapped")
    if ago is None or ago <= 0:
        return None
    return int(round((time.time() - ago) / 60) * 60)


def parse_interfaces(root: ET.Element, only_physical_ports: bool = True) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for phy in root.iter("physical-interface"):
        name = _t(phy, "name")
        if not name or (only_physical_ports and not PHYSICAL_PORT.match(name)):
            continue
        ts = phy.find("traffic-statistics")
        ie = phy.find("input-error-list")
        oe = phy.find("output-error-list")
        eth = phy.find("ethernet-mac-statistics")
        out[name] = {
            "name": name,
            "description": _t(phy, "description"),
            "admin_up": (_t(phy, "admin-status") or "").lower() == "up",
            "oper_up": (_t(phy, "oper-status") or "").lower() == "up",
            "speed_mbps": _speed_mbps(_t(phy, "speed")),
            "configured_speed": _t(phy, "speed"),
            "duplex": _t(phy, "duplex") or _t(phy, "link-mode"),
            "mtu": _int(_t(phy, "mtu")),
            "mac": _t(phy, "current-physical-address") or _t(phy, "hardware-physical-address"),
            # junos:seconds on interface-flapped is "seconds ago"; turn it into an epoch, stable to the minute
            "last_flapped": _flapped(phy),
            "carrier_transitions": _int(_t(oe, "carrier-transitions")),
            "rx_bytes": _int(_t(ts, "input-bytes")),
            "tx_bytes": _int(_t(ts, "output-bytes")),
            "rx_packets": _int(_t(ts, "input-packets")),
            "tx_packets": _int(_t(ts, "output-packets")),
            "rx_bps": _int(_t(ts, "input-bps")),
            "tx_bps": _int(_t(ts, "output-bps")),
            "rx_pps": _int(_t(ts, "input-pps")),
            "tx_pps": _int(_t(ts, "output-pps")),
            "rx_errors": _int(_t(ie, "input-errors")),
            "rx_drops": _int(_t(ie, "input-drops")),
            "tx_errors": _int(_t(oe, "output-errors")),
            "tx_drops": _int(_t(oe, "output-drops")),
            "crc_errors": _int(_t(eth, "input-crc-errors")),
            "rx_broadcast": _int(_t(eth, "input-broadcasts")),
            "rx_multicast": _int(_t(eth, "input-multicasts")),
        }
    return out


def parse_optics(root: ET.Element) -> dict[str, dict[str, Any]]:
    out = {}
    for phy in root.iter("physical-interface"):
        name = _t(phy, "name")
        d = phy.find("optics-diagnostics")
        if not name or d is None or d.find("optic-diagnostics-not-available") is not None:
            continue
        rx = (_t(d, "rx-signal-avg-optical-power-dbm") or _t(d, "laser-rx-optical-power-dbm")
              or _t(d, "receiver-signal-avg-optical-power-dbm"))
        out[name] = {
            "tx_power_dbm": _num(_t(d, "laser-output-power-dbm")),
            "rx_power_dbm": _num(rx),
            "temperature": _celsius(d, "module-temperature"),
            "voltage": _num(_t(d, "module-voltage")),
            "bias_ma": _num(_t(d, "laser-bias-current")),
        }
    return out


# ------------------------------------------------------------------------------------------------ PoE

def _first(el: ET.Element, *tags: str) -> str | None:
    for t in tags:
        v = _t(el, t)
        if v is not None:
            return v
    return None


def parse_poe_interfaces(root: ET.Element) -> dict[str, dict[str, Any]]:
    out = {}
    for item in root.iter():
        name = _first(item, "interface-name", "poe-interface-name")
        if name is None or item.find("interface-name") is None and item.find("poe-interface-name") is None:
            continue
        if not PHYSICAL_PORT.match(name):
            continue
        out[name] = {
            "enabled": (_first(item, "interface-enabled", "poe-interface-admin") or "").lower() in ("enabled", "enable"),
            "status": _first(item, "interface-status", "poe-interface-status"),
            "power_w": _num(_first(item, "interface-power", "poe-interface-power")),
            "max_power_w": _num(_first(item, "interface-power-limit", "interface-max-power", "poe-interface-max-power")),
            "class": _first(item, "interface-class", "poe-interface-class"),
            "priority": _first(item, "interface-priority", "poe-interface-priority"),
        }
    return out


def parse_poe_controller(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for item in root.iter():
        num = _first(item, "controller-number", "controller-fpc-slot")
        if num is None or item.find("controller-number") is None and item.find("controller-fpc-slot") is None:
            continue
        out.append({
            "key": num,
            "power_limit_w": _num(_first(item, "controller-power-limit", "controller-maximum-power")),
            "power_w": _num(_first(item, "controller-power", "controller-power-consumption")),
            "guard_band_w": _num(_t(item, "controller-guard-band")),
            "status": _t(item, "controller-status"),
        })
    return out


# --------------------------------------------------------------------------------------------- tables

def parse_mac_count(root: ET.Element) -> int:
    n = 0
    for tag in ("l2ng-mac-entry", "mac-table-entry"):
        for e in root.iter(tag):
            mac = _first(e, "l2ng-l2-mac-address", "mac-address") or ""
            if mac and mac != "*":
                n += 1
    return n


def parse_arp(root: ET.Element) -> int:
    count = _int(_t(root.find(".//arp-table-information"), "arp-entry-count"))
    if count is not None:
        return count
    return len(list(root.iter("arp-table-entry")))


def parse_lldp(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for n in root.iter("lldp-neighbor-information"):
        out.append({
            "local_port": _first(n, "lldp-local-port-id", "lldp-local-interface"),
            "chassis_id": _t(n, "lldp-remote-chassis-id"),
            "port": _first(n, "lldp-remote-port-description", "lldp-remote-port-id"),
            "system": _t(n, "lldp-remote-system-name"),
        })
    return out


def parse_storage(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for member, fs in multi_re(root, "filesystem"):
        mount = _t(fs, "mounted-on")
        out.append({
            "member": member,
            "mount": mount,
            "device": _t(fs, "filesystem-name"),
            "used_percent": _num(_t(fs, "used-percent")),
            "total_blocks": _int(_t(fs, "total-blocks")),
            "used_blocks": _int(_t(fs, "used-blocks")),
        })
    return out


def parse_virtual_chassis(root: ET.Element) -> list[dict[str, Any]]:
    out = []
    for m in root.iter("member"):
        mid = _t(m, "member-id")
        if mid is None:
            continue
        out.append({
            "id": mid,
            "status": _t(m, "member-status"),
            "serial": _t(m, "member-serial-number"),
            "model": _t(m, "member-model"),
            "role": (_t(m, "member-role") or "").rstrip("*") or None,
            "priority": _int(_t(m, "member-priority")),
        })
    return out


def parse_commit(root: ET.Element) -> dict[str, Any] | None:
    h = next(iter(root.iter("commit-history")), None)
    if h is None:
        return None
    return {
        "time": _seconds(h, "date-time"),
        "user": _t(h, "user"),
        "client": _t(h, "client"),
        "comment": _t(h, "log"),
    }
