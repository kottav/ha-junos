"""Data update coordinator for a Junos device."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    Credentials,
    JunosAuthError,
    JunosClient,
    JunosConnectionError,
    JunosError,
    JunosHostKeyError,
)
from .const import CONF_HOST_KEY, CONF_PASSPHRASE, CONF_PRIVATE_KEY, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

JunosConfigEntry = ConfigEntry["JunosCoordinator"]


def client_from_entry_data(data: dict[str, Any]) -> JunosClient:
    creds = Credentials(
        username=data[CONF_USERNAME],
        password=data.get(CONF_PASSWORD) or None,
        private_key=data.get(CONF_PRIVATE_KEY) or None,
        passphrase=data.get(CONF_PASSPHRASE) or None,
    )
    return JunosClient(data[CONF_HOST], data[CONF_PORT], creds, host_key=data.get(CONF_HOST_KEY))


class JunosCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one switch."""

    config_entry: JunosConfigEntry

    def __init__(self, hass: HomeAssistant, entry: JunosConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.data[CONF_HOST]}",
                         update_interval=timedelta(seconds=interval))
        self.client = client_from_entry_data(dict(entry.data))

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.poll()
        except (JunosAuthError, JunosHostKeyError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except JunosConnectionError as err:
            raise UpdateFailed(f"Cannot reach {self.client.nc.host}: {err}") from err
        except JunosError as err:
            raise UpdateFailed(str(err)) from err

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.close()

    # ---- helpers used by the platforms ----------------------------------------------------------------

    @property
    def members(self) -> list[str]:
        """Distinct virtual chassis members seen in per-member data (['fpc0', 'fpc1'] or [])."""
        seen: list[str] = []
        for key in ("route_engines", "environment", "fans"):
            for item in (self.data or {}).get(key) or []:
                m = item.get("member")
                if m and m not in seen:
                    seen.append(m)
        return seen

    @property
    def is_virtual_chassis(self) -> bool:
        return len(self.members) > 1
