"""E.ON Next smart prepay meters for Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EonNextClient
from .const import CONF_REFRESH_TOKEN
from .coordinator import EonNextPrepayConfigEntry, EonNextPrepayCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EonNextPrepayConfigEntry) -> bool:
    """Set up E.ON Next Prepay from a config entry."""

    @callback
    def _save_refresh_token(token: str) -> None:
        # Auth0 rotates the refresh token on every use - losing it means a re-login
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = EonNextClient(
        async_get_clientsession(hass),
        entry.data[CONF_REFRESH_TOKEN],
        _save_refresh_token,
    )
    coordinator = EonNextPrepayCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EonNextPrepayConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
