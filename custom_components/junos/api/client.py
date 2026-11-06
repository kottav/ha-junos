"""High-level Junos poller: runs the RPCs, parses them, computes interface rates."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from . import parsers as p
from .netconf import Credentials, JunosNetconf, JunosRpcError

_LOGGER = logging.getLogger(__name__)

# name -> (rpc body, parser, slow). Slow RPCs run at most every SLOW_INTERVAL seconds.
RPCS: dict[str, tuple[str, Callable[..., Any], bool]] = {
    "software": ("<get-software-information/>", p.parse_software, True),
    "inventory": ("<get-chassis-inventory/>", p.parse_inventory, True),
    "route_engines": ("<get-route-engine-information/>", p.parse_route_engines, False),
    "environment": ("<get-environment-information/>", p.parse_environment, False),
    "fans": ("<get-fan-information/>", p.parse_fans, False),
    "chassis_alarms": ("<get-alarm-information/>", p.parse_alarms, False),
    "system_alarms": ("<get-system-alarm-information/>", p.parse_alarms, False),
    "interfaces": ("<get-interface-information><extensive/></get-interface-information>",
                   p.parse_interfaces, False),
    "optics": ("<get-interface-optics-diagnostics-information/>", p.parse_optics, False),
    "poe_interfaces": ("<get-poe-interface-information/>", p.parse_poe_interfaces, False),
    "poe_controller": ("<get-poe-controller-information/>", p.parse_poe_controller, False),
    "mac_count": ("<get-ethernet-switching-table-information/>", p.parse_mac_count, True),
    "arp_count": ("<get-arp-table-information><no-resolve/></get-arp-table-information>", p.parse_arp, True),
    "lldp": ("<get-lldp-neighbors-information/>", p.parse_lldp, True),
    "storage": ("<get-system-storage/>", p.parse_storage, True),
    "virtual_chassis": ("<get-virtual-chassis-information/>", p.parse_virtual_chassis, True),
    "commit": ("<get-commit-information/>", p.parse_commit, True),
}
SLOW_INTERVAL = 300.0


class JunosClient:
    """Polls one switch. Remembers which RPCs the platform does not support and stops asking."""

    def __init__(self, host: str, port: int, creds: Credentials, host_key: str | None = None,
                 timeout: float = 30.0) -> None:
        self.nc = JunosNetconf(host, port, creds, host_key=host_key, timeout=timeout)
        self.unsupported: set[str] = set()
        self._slow_at = 0.0
        self._last: dict[str, Any] = {}
        self._prev_counters: dict[str, tuple[float, int | None, int | None, int | None, int | None]] = {}
        self.timings: dict[str, float] = {}

    async def close(self) -> None:
        await self.nc.close()

    async def run(self, name: str) -> Any:
        body, parser, _ = RPCS[name]
        start = time.monotonic()
        root = await self.nc.rpc(body, name)
        self.timings[name] = round(time.monotonic() - start, 3)
        return parser(root)

    async def identify(self) -> dict[str, Any]:
        """Used by the config flow: model, version, hostname, serial."""
        info = await self.run("software")
        info["serial"] = (await self.run("inventory")).get("serial")
        return info

    async def poll(self) -> dict[str, Any]:
        now = time.monotonic()
        slow_due = now - self._slow_at >= SLOW_INTERVAL
        data: dict[str, Any] = dict(self._last)
        for name, (_, _, slow) in RPCS.items():
            if name in self.unsupported or (slow and not slow_due):
                continue
            try:
                data[name] = await self.run(name)
            except JunosRpcError as err:
                if err.unsupported:
                    _LOGGER.debug("%s not supported on %s, skipping from now on: %s", name, self.nc.host, err)
                    self.unsupported.add(name)
                    data.pop(name, None)
                else:
                    _LOGGER.warning("%s failed on %s: %s", name, self.nc.host, err)
        if slow_due:
            self._slow_at = now
        self._add_rates(data.get("interfaces") or {}, now)
        data["polled_at"] = time.time()
        self._last = data
        return data

    def _add_rates(self, ifaces: dict[str, dict[str, Any]], now: float) -> None:
        """Average bit/packet rates since the previous poll, from the octet/packet counters.

        Falls back to the switch's own (few-second) bps/pps estimate on the first poll or after a counter reset.
        """
        for name, i in ifaces.items():
            cur = (now, i.get("rx_bytes"), i.get("tx_bytes"), i.get("rx_packets"), i.get("tx_packets"))
            prev = self._prev_counters.get(name)
            self._prev_counters[name] = cur
            rates = {"rx_rate": i.get("rx_bps"), "tx_rate": i.get("tx_bps"),
                     "rx_pkt_rate": i.get("rx_pps"), "tx_pkt_rate": i.get("tx_pps")}
            if prev is not None and cur[0] > prev[0]:
                dt = cur[0] - prev[0]
                for idx, key, mult in ((1, "rx_rate", 8), (2, "tx_rate", 8), (3, "rx_pkt_rate", 1), (4, "tx_pkt_rate", 1)):
                    a, b = prev[idx], cur[idx]
                    if a is not None and b is not None and b >= a:
                        rates[key] = round((b - a) * mult / dt)
            i.update(rates)
