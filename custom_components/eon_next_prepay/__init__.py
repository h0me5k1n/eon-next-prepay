"""E.ON Next smart prepay meters for Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EonNextClient
from .const import CONF_API_KEY, CONF_REFRESH_TOKEN, DOMAIN, ISSUE_SWITCH_TO_API_KEY
from .coordinator import EonNextPrepayConfigEntry, EonNextPrepayCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


def switch_issue_id(entry: EonNextPrepayConfigEntry) -> str:
    """Repair issue id for one entry's switch to an API key."""
    return f"{ISSUE_SWITCH_TO_API_KEY}_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: EonNextPrepayConfigEntry) -> bool:
    """Set up E.ON Next Prepay from a config entry."""

    @callback
    def _save_refresh_token(token: str) -> None:
        # Auth0 rotates the refresh token on every use - losing it means a re-login.
        # Once the entry has switched to an API key the old chain doesn't matter.
        if CONF_API_KEY in entry.data:
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    session = async_get_clientsession(hass)
    if api_key := entry.data.get(CONF_API_KEY):
        client = EonNextClient(session, api_key=api_key)
    else:
        client = EonNextClient(session, entry.data[CONF_REFRESH_TOKEN], _save_refresh_token)
    coordinator = EonNextPrepayCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    if client.uses_api_key:
        ir.async_delete_issue(hass, DOMAIN, switch_issue_id(entry))
    else:
        # opt-in: creating a key changes the user's E.ON account, so ask first
        ir.async_create_issue(
            hass,
            DOMAIN,
            switch_issue_id(entry),
            data={"entry_id": entry.entry_id},
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SWITCH_TO_API_KEY,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EonNextPrepayConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EonNextPrepayConfigEntry) -> None:
    """Clean up the repair issue when the integration is removed."""
    ir.async_delete_issue(hass, DOMAIN, switch_issue_id(entry))
