"""Run every RPC the integration uses against a switch and print the parsed result.

Useful for checking a new platform before adding it to Home Assistant, and for bug reports.

    python scripts/probe.py switch.example.lan --user homeassistant --key ~/.ssh/id_ed25519 [--dump fixtures/]
    python scripts/probe.py switch.example.lan --user homeassistant --password   (prompts)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "junos"))

from api.client import RPCS, JunosClient  # noqa: E402
from api.netconf import Credentials, JunosRpcError  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=830)
    ap.add_argument("--user", default="homeassistant")
    ap.add_argument("--key", help="private key file")
    ap.add_argument("--password", action="store_true", help="prompt for a password")
    ap.add_argument("--dump", help="directory to write raw RPC replies (XML) into")
    ap.add_argument("--only", help="comma separated RPC names")
    a = ap.parse_args()
    key = open(os.path.expanduser(a.key), encoding="utf-8").read() if a.key else None
    pw = getpass.getpass() if a.password else None
    c = JunosClient(a.host, a.port, Credentials(a.user, password=pw, private_key=key))
    names = a.only.split(",") if a.only else list(RPCS)
    try:
        await c.nc.connect()
        print("host key:", c.nc.server_host_key)
        for name in names:
            body, parser, _ = RPCS[name]
            t = time.monotonic()
            try:
                root = await c.nc.rpc(body, name)
            except JunosRpcError as err:
                print(f"== {name}: RPC ERROR (unsupported={err.unsupported}): {err.message}")
                continue
            dt = time.monotonic() - t
            if a.dump:
                os.makedirs(a.dump, exist_ok=True)
                with open(os.path.join(a.dump, f"{name}.xml"), "w", encoding="utf-8") as f:
                    f.write(ET.tostring(root, encoding="unicode"))
            parsed = parser(root)
            text = json.dumps(parsed, indent=1, default=str)
            print(f"== {name} ({dt:.2f}s, {len(ET.tostring(root))} bytes)")
            print(text if len(text) < 4000 else text[:4000] + "\n...")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
