# Juniper Junos for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

Monitor Juniper switches (EX, QFX, and also SRX/MX) from Home Assistant: per-port throughput, link
state, errors, temperatures, fan RPM, power supplies, chassis alarms, CPU/memory, PoE, optics (DOM),
MAC/ARP table sizes, LLDP neighbours and more.

It talks to the switch over **NETCONF/SSH** with a **read-only** account, polls locally (no cloud) and
needs nothing installed on the switch.

## Features

| Area | Entities |
| --- | --- |
| **Ports** (every front-panel port, breakout `xe-0/0/0:2` and `aeN`) | RX/TX throughput (bit/s, averaged over the poll interval), link up/down, RX/TX errors; disabled by default: RX/TX byte totals (for Energy-style statistics), packet rates, drops, CRC errors, link flaps, link speed, last link change |
| **System** | CPU utilization, memory utilization, routing engine and CPU temperature, load average, last boot |
| **Environment** | Every chassis temperature sensor, fan speed (RPM), fan / power supply OK binary sensors |
| **Alarms** | Active alarm count (with descriptions), Major alarm / Minor alarm problem sensors |
| **PoE** (PoE models) | Total PoE draw, PoE budget, per-port PoE power |
| **Optics** | Per-transceiver RX/TX power (dBm) and module temperature |
| **Tables** | MAC table entries, ARP entries, LLDP neighbours (list as attribute) |
| **Other** | Storage used, virtual chassis member role/health, last configuration change (with user) |

Anything the platform doesn't support (e.g. PoE on a QFX) is detected on the first poll and skipped.
Virtual chassis members get their own sub-devices.

## Installation

### 1. Prepare the switch

Follow **[docs/juniper-setup.md](docs/juniper-setup.md)**. The short version (in `configure` mode):

```
set system services netconf ssh
set system login user homeassistant class read-only
set system login user homeassistant authentication plain-text-password
commit and-quit
```

### 2. Install the integration

**HACS (recommended):** HACS → ⋮ → *Custom repositories* → add `https://github.com/kottav/ha-junos`
with category *Integration*. Then search for **Juniper Junos**, install it and restart Home Assistant.

**Manual:** copy `custom_components/junos` into your Home Assistant `config/custom_components/` folder
and restart.

### 3. Add the switch

*Settings → Devices & services → Add integration → Juniper Junos*, then fill in:

| Field | Value |
| --- | --- |
| Host | switch hostname or management IP |
| NETCONF port | `830` |
| Username | `homeassistant` |
| Password **or** SSH private key | whichever you configured. Paste the whole key including the `BEGIN`/`END` lines |

The switch's SSH host key is stored on first connect and checked on every connect after that. If it
changes (RE replacement, zeroize), Home Assistant asks you to re-authenticate and accept the new key.

### 4. Options

*Configure* on the integration lets you change the polling interval (default 30 s; throughput is the
average over this interval) and choose which ports get entities. Port descriptions from the switch are
shown in that list.

## Example dashboard card

```yaml
type: history-graph
title: Uplink
hours_to_show: 24
entities:
  - sensor.core_switch_xe_0_0_0_0_rx_throughput
  - sensor.core_switch_xe_0_0_0_0_tx_throughput
```

```yaml
type: entities
title: Switch health
entities:
  - binary_sensor.core_switch_major_alarm
  - sensor.core_switch_active_alarms
  - sensor.core_switch_cpu_utilization
  - sensor.core_switch_memory_utilization
  - sensor.core_switch_routing_engine_temperature
```

## Example automation

```yaml
alias: Switch alarm
triggers:
  - trigger: state
    entity_id: binary_sensor.core_switch_major_alarm
    to: "on"
actions:
  - action: notify.notify
    data:
      message: >
        {{ state_attr('binary_sensor.core_switch_major_alarm', 'alarms') | join(', ') }}
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "SSH worked but NETCONF is off" | `set system services netconf ssh` and `commit` |
| "rejected the username, password or key" | Check the user exists (`show configuration system login`), and that you pasted the *private* key |
| "No answer from …:830" | Home Assistant can't reach the switch's management address. Usual cause: the management port sits on its own VLAN and the router only lets some hosts in. Allow the HA host → switch TCP 830, and test from the HA host with `timeout 5 bash -c '</dev/tcp/SWITCH/830' && echo open` |
| Some sensors missing | The platform doesn't have that data. Download diagnostics (⋮ on the device page): `unsupported_rpcs` lists what was skipped |
| Minor alarm "Rescue configuration is not set" | `request system configuration rescue save` on the switch |

Debug logging:

```yaml
logger:
  logs:
    custom_components.junos: debug
```

`scripts/probe.py` runs every RPC from a PC and prints the parsed result. Attach its output (and
diagnostics) to bug reports, ideally with `--dump fixtures/` so we can add your platform to the tests.

## Development

```bash
python -m venv .venv && .venv/bin/pip install asyncssh pytest pytest-asyncio
.venv/bin/pytest tests
```

`custom_components/junos/api/` has no Home Assistant imports: the NETCONF client
(`netconf.py`), the parsers (`parsers.py`, tested against recorded replies in `tests/fixtures/`) and the
poller (`client.py`).

## Licence

MIT. Not affiliated with Juniper Networks. Junos is a trademark of Juniper Networks, Inc.
