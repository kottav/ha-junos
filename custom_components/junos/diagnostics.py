"""Diagnostics download for Juniper Junos (credentials redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import CONF_HOST_KEY, CONF_PASSPHRASE, CONF_PRIVATE_KEY
from .coordinator import JunosConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_PRIVATE_KEY, CONF_PASSPHRASE, CONF_HOST_KEY, "serial", "mac", "chassis_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: JunosConfigEntry) -> dict[str, Any]:
    c = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "unsupported_rpcs": sorted(c.client.unsupported),
        "rpc_timings_s": c.client.timings,
        "data": async_redact_data(c.data or {}, TO_REDACT),
    }
