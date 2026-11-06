"""JunosClient: rate computation, unsupported-RPC memory, NETCONF framing and errors."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "custom_components", "junos"))

from api import client as client_mod  # noqa: E402
from api.client import JunosClient  # noqa: E402
from api.netconf import EOM, Credentials, JunosNetconf, JunosRpcError  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "qfx5200")


class FakeReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, n):
        return self.chunks.pop(0) if self.chunks else ""


class FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, s):
        self.sent.append(s)


def _nc_with_replies(*replies: str) -> JunosNetconf:
    nc = JunosNetconf("sw", 830, Credentials("u", password="p"))
    nc._conn = object()  # pretend connected
    # split replies awkwardly across reads to exercise framing
    blob = "".join(r + EOM for r in replies)
    nc._reader = FakeReader([blob[i:i + 7] for i in range(0, len(blob), 7)])
    nc._writer = FakeWriter()
    return nc


def test_rpc_framing_and_namespaces():
    reply = ('<?xml version="1.0"?><rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" '
             'xmlns:junos="http://xml.juniper.net/junos/21.2R0/junos" message-id="1">'
             '<route-engine-information xmlns="http://xml.juniper.net/junos/21.2R0/junos-chassis">'
             '<route-engine><temperature junos:celsius="41">41 degrees C</temperature></route-engine>'
             '</route-engine-information></rpc-reply>')
    nc = _nc_with_replies(reply)
    root = asyncio.run(nc.rpc("<get-route-engine-information/>"))
    el = root.find("route-engine-information/route-engine/temperature")
    assert el is not None and el.attrib["celsius"] == "41"
    assert nc._writer.sent[0].startswith('<rpc message-id="1"><get-route-engine-information/></rpc>')


def test_rpc_error_raises_and_warning_ignored():
    err = ('<rpc-reply><rpc-error><error-severity>error</error-severity>'
           '<error-message>syntax error</error-message></rpc-error></rpc-reply>')
    warn = ('<rpc-reply><rpc-error><error-severity>warning</error-severity>'
            '<error-message>meh</error-message></rpc-error><ok/></rpc-reply>')
    nc = _nc_with_replies(err, warn)
    with pytest.raises(JunosRpcError) as e:
        asyncio.run(nc.rpc("<get-foo/>"))
    assert e.value.unsupported
    asyncio.run(nc.rpc("<get-bar/>"))  # no raise


def test_poll_rates_and_unsupported(monkeypatch):
    c = JunosClient("sw", 830, Credentials("u", password="p"))
    calls = []
    counters = iter([(1000, 2000), (1000 + 125_000 * 30, 2000 + 250_000 * 30)])  # 1 Mbit/s rx, 2 Mbit/s tx

    async def fake_run(name):
        calls.append(name)
        if name == "poe_interfaces":
            raise JunosRpcError(name, "command is not valid on the current system")
        if name == "interfaces":
            rx, tx = next(counters)
            return {"xe-0/0/1": {"rx_bytes": rx, "tx_bytes": tx, "rx_packets": 1, "tx_packets": 1,
                                 "rx_bps": 7, "tx_bps": 8, "rx_pps": 0, "tx_pps": 0}}
        return {}

    t = [1000.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: t[0])
    c.run = fake_run
    d1 = asyncio.run(c.poll())
    assert d1["interfaces"]["xe-0/0/1"]["rx_rate"] == 7  # first poll: switch's own estimate
    assert "poe_interfaces" in c.unsupported
    calls.clear()
    t[0] += 30
    d2 = asyncio.run(c.poll())
    assert d2["interfaces"]["xe-0/0/1"]["rx_rate"] == 1_000_000
    assert d2["interfaces"]["xe-0/0/1"]["tx_rate"] == 2_000_000
    assert "poe_interfaces" not in calls  # not asked again
    assert "software" not in calls  # slow RPC, not due yet
