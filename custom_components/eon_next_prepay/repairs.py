"""Repairs for E.ON Next Prepay: an opt-in switch from a refresh token to an API key."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .config_flow import ApiKeySteps
from .const import CONF_API_KEY
from .coordinator import EonNextPrepayConfigEntry


class SwitchToApiKeyFlow(ApiKeySteps, RepairsFlow):
    """Create an API key using the entry's current (still working) refresh token.

    Nothing changes on the user's E.ON account until they confirm.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _entry(self) -> EonNextPrepayConfigEntry | None:
        return self.hass.config_entries.async_get_entry(self._entry_id)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="already_switched")
        if CONF_API_KEY in entry.data:
            # key saved, but the flow was closed before the reload - finish it
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="already_switched")
        if entry.state is not ConfigEntryState.LOADED:
            # the refresh token has already expired; the reauth prompt covers this
            return self.async_abort(reason="not_loaded")
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))
        self._client = entry.runtime_data.client
        self._account_number = entry.unique_id or ""
        return await self._async_create_api_key()

    async def _async_key_created(self, api_key: str) -> None:
        # save now: E.ON won't show this key again
        if entry := self._entry():
            self.hass.config_entries.async_update_entry(entry, data={CONF_API_KEY: api_key})

    async def _async_finish_api_key(self, api_key: str) -> FlowResult:
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="already_switched")
        if entry.data.get(CONF_API_KEY) != api_key:
            self.hass.config_entries.async_update_entry(entry, data={CONF_API_KEY: api_key})
        # the issue is removed when the reloaded entry starts up with the key
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})

    async def _async_finish_refresh_token(self) -> FlowResult:
        # nothing changed - leave the issue up so they can switch later
        return self.async_abort(reason="kept_refresh_token")

    async def _async_create_failed(self) -> FlowResult:
        return self.async_abort(reason="create_failed")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for a switch_to_api_key issue."""
    assert data is not None
    return SwitchToApiKeyFlow(str(data["entry_id"]))
