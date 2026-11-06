"""Parser tests against real (anonymised) replies from a QFX5200-32C on Junos 21.2R2."""

from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "custom_components", "junos"))

from api import parsers as p  # noqa: E402
from api.netconf import JunosRpcError, strip_ns  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "qfx5200")


def load(name: str) -> ET.Element:
    return strip_ns(ET.parse(os.path.join(FIX, f"{name}.xml")).getroot())


def test_software():
    s = p.parse_software(load("software"))
    assert s["model"] == "qfx5200-32c-32q"
    assert s["version"] == "21.2R2-S1.5"
    assert s["hostname"] is None  # no host-name configured on this box


def test_inventory():
    inv = p.parse_inventory(load("inventory"))
    assert inv["serial"] == "SERIAL0001"
    assert any(m["name"] == "Power Supply 1" for m in inv["modules"])


def test_route_engine():
    (re0,) = p.parse_route_engines(load("route_engines"))
    assert re0["key"] == "re0"
    assert re0["cpu_utilization"] == 3.0
    assert re0["memory_utilization"] == 13.0
    assert re0["temperature"] == 28.0
    assert re0["start_time"] == 1750203425
    assert re0["load_1"] == 0.17


def test_environment_carries_class_forward():
    env = p.parse_environment(load("environment"))
    classes = {e["class"] for e in env}
    assert {"power", "temp", "fans"} <= classes
    temps = [e for e in env if e["temperature"] is not None]
    assert len(temps) >= 12
    die = next(e for e in env if e["name"] == "FPC 0 Sensor CPU Die Temp")
    assert die["temperature"] == 38.0 and die["key"] == "fpc_0_sensor_cpu_die_temp"


def test_fans_rpm():
    fans = p.parse_fans(load("fans"))
    assert len(fans) == 10
    assert all(f["rpm"] and 3000 < f["rpm"] < 10000 for f in fans)


def test_alarms_merge_dedupes():
    ch = p.parse_alarms(load("chassis_alarms"))
    sy = p.parse_alarms(load("system_alarms"))
    assert ch[0]["class"] == "major"
    assert ch[0]["description"] == "FPC Management1 Ethernet Link Down"
    assert len(p.merge_alarms(ch, sy)) == 1


def test_interfaces():
    ifs = p.parse_interfaces(load("interfaces"))
    # channelised breakout ports and aggregates count; internal ifds (sxe, pfe, em, vme, gr) don't
    assert "xe-0/0/0:2" in ifs and "et-0/0/1:3" in ifs and "ae0" in ifs and "xe-0/0/17" in ifs
    assert not any(n.startswith(("sxe", "pfe", "gr-", "em", "vme", "lo")) for n in ifs)
    x = ifs["xe-0/0/17"]
    assert x["description"] == "downlink-1"
    assert x["speed_mbps"] == 10000
    assert x["oper_up"] and x["admin_up"]
    assert x["rx_bytes"] == 100217533815690
    assert x["rx_errors"] == 111 and x["rx_drops"] == 21276
    assert x["crc_errors"] == 111
    assert x["carrier_transitions"] == 101
    # interface-flapped junos:seconds means "seconds ago", so the timestamp must be in the past
    assert x["last_flapped"] < time.time() and x["last_flapped"] > time.time() - 400 * 86400
    assert ifs["ae0"]["speed_mbps"] == 20000


def test_optics():
    o = p.parse_optics(load("optics"))
    assert o["xe-0/0/17"]["rx_power_dbm"] == -3.98
    assert o["xe-0/0/17"]["temperature"] == 26.0


def test_tables():
    assert p.parse_mac_count(load("mac_count")) == 67
    assert p.parse_arp(load("arp_count")) == 5
    lldp = p.parse_lldp(load("lldp"))
    assert any(n["local_port"] == "em0" for n in lldp)


def test_storage_and_vc_and_commit():
    st = p.parse_storage(load("storage"))
    assert any(f["mount"] == "/.mount" and f["used_percent"] == 18 for f in st)
    vc = p.parse_virtual_chassis(load("virtual_chassis"))
    assert vc == [{"id": "0", "status": "Prsnt", "serial": "SERIAL0001", "model": "qfx5200-32c-32q",
                   "role": "Master", "priority": 128}]
    c = p.parse_commit(load("commit"))
    assert c["user"] == "root" and c["time"] == 1793988281


def test_poe_bare_error_is_unsupported():
    # Non-PoE switches answer with <error><message>command is not valid...</message></error>
    err = JunosRpcError("poe_interfaces", "command is not valid on the current system (chassis-control)")
    assert err.unsupported
    assert p.parse_poe_interfaces(load("poe_interfaces")) == {}


@pytest.mark.parametrize("name,ok", [
    ("ge-0/0/0", True), ("mge-1/0/47", True), ("xe-0/0/0:2", True), ("et-0/0/31", True), ("ae12", True),
    ("sxe-0/0/0", False), ("gr-0/0/0", False), ("em0", False), ("irb", False), ("vme", False),
])
def test_port_regex(name, ok):
    assert bool(p.PHYSICAL_PORT.match(name)) is ok
