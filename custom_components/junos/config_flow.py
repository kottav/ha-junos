"""Config flow for Juniper Junos."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import JunosAuthError, JunosConnectionError, JunosError, JunosHostKeyError
from .api.parsers import PHYSICAL_PORT
from .const import (
    CONF_HOST_KEY,
    CONF_INTERFACES,
    CONF_PASSPHRASE,
    CONF_PRIVATE_KEY,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import JunosConfigEntry, client_from_entry_data

_LOGGER = logging.getLogger(__name__)

_PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
_KEY = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _credentials_schema(defaults: Mapping[str, Any]) -> dict:
    return {
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
        vol.Optional(CONF_PASSWORD): _PASSWORD,
        vol.Optional(CONF_PRIVATE_KEY): _KEY,
        vol.Optional(CONF_PASSPHRASE): _PASSWORD,
    }


async def _validate(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Connect, identify the switch. Returns (info, host_key). Raises JunosError subclasses."""
    client = client_from_entry_data(data)
    try:
        info = await client.identify()
        return info, client.nc.server_host_key or ""
    finally:
        await client.close()


def _error_key(err: Exception) -> str:
    if isinstance(err, JunosHostKeyError):
        return "host_key_changed"
    if isinstance(err, JunosAuthError):
        return "invalid_auth"
    if isinstance(err, JunosConnectionError):
        return "netconf_refused" if "subsystem" in str(err).lower() else "cannot_connect"
    return "unknown"


class JunosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Junos device."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            data = {k: v.strip() if isinstance(v, str) and k != CONF_PRIVATE_KEY else v for k, v in user_input.items()}
            if not data.get(CONF_PASSWORD) and not data.get(CONF_PRIVATE_KEY):
                errors["base"] = "no_credentials"
            else:
                try:
                    info, host_key = await _validate(data)
                except JunosError as err:
                    errors["base"] = _error_key(err)
                    placeholders["error"] = str(err)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Unexpected error validating %s", data.get(CONF_HOST))
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(info.get("serial") or data[CONF_HOST])
                    self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
                    data[CONF_HOST_KEY] = host_key
                    title = info.get("hostname") or data[CONF_HOST]
                    return self.async_create_entry(title=title, data=data)
        defaults = user_input or {}
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)),
            **_credentials_schema(defaults),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """New credentials and/or accept a changed SSH host key (e.g. after an RE replacement or zeroize)."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders = {"host": entry.data[CONF_HOST], "error": ""}
        if user_input is not None:
            data = {**entry.data, **user_input, CONF_HOST_KEY: None}  # re-learn the host key
            if not data.get(CONF_PASSWORD) and not data.get(CONF_PRIVATE_KEY):
                errors["base"] = "no_credentials"
            else:
                try:
                    _, host_key = await _validate(data)
                except JunosError as err:
                    errors["base"] = _error_key(err)
                    placeholders["error"] = str(err)
                else:
                    data[CONF_HOST_KEY] = host_key
                    return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema(_credentials_schema(entry.data)),
                                    errors=errors, description_placeholders=placeholders)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: JunosConfigEntry) -> OptionsFlow:
        return JunosOptionsFlow()


class JunosOptionsFlow(OptionsFlow):
    """Polling interval and which ports get entities."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)
        entry = self.config_entry
        coordinator = getattr(entry, "runtime_data", None)
        ports = sorted((coordinator.data or {}).get("interfaces", {}) if coordinator else [],
                       key=_port_sort_key)
        current = entry.options.get(CONF_INTERFACES)
        if current is None:
            current = ports
        options = []
        for name in sorted(set(ports) | set(current), key=_port_sort_key):
            desc = (coordinator.data or {}).get("interfaces", {}).get(name, {}).get("description") if coordinator else None
            options.append(selector.SelectOptionDict(value=name, label=f"{name} ({desc})" if desc else name))
        schema = vol.Schema({
            vol.Required(CONF_SCAN_INTERVAL, default=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                selector.NumberSelector(selector.NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, step=1, unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX)),
            vol.Optional(CONF_INTERFACES, default=current): selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, multiple=True, mode=selector.SelectSelectorMode.LIST)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)


def _port_sort_key(name: str) -> tuple:
    m = PHYSICAL_PORT.match(name)
    if not m:
        return (2, 0, 0, 0, 0, name)
    if m.group(6) is not None:  # aeN
        return (1, int(m.group(6)), 0, 0, 0, "ae")
    return (0, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5) or 0), m.group(1))
