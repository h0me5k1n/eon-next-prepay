"""Config flow for E.ON Next Prepay."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EonNextApiError, EonNextAuthError, EonNextClient
from .const import (
    CONF_LOW_CREDIT_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_LOW_CREDIT_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import EonNextPrepayConfigEntry, find_prepay_meters

_LOGGER = logging.getLogger(__name__)

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REFRESH_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


class NoPrepayMeters(Exception):
    """The account has no smart meters in prepayment mode."""


class EonNextPrepayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Next Prepay."""

    VERSION = 1

    async def _validate(self, refresh_token: str) -> tuple[str, str]:
        """Check the token works and the account has prepay meters.

        Returns (account number, rotated refresh token) - the token passed in
        is spent by the check, so only the returned one is usable.
        """
        client = EonNextClient(async_get_clientsession(self.hass), refresh_token.strip())
        accounts = await client.async_get_accounts()
        meters, _ = find_prepay_meters(accounts)
        if not meters:
            raise NoPrepayMeters
        return meters[0].account_number, client.refresh_token

    async def _async_handle_token(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> tuple[str, str] | ConfigFlowResult:
        """Validate submitted input, or show the form (again) with errors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                return await self._validate(user_input[CONF_REFRESH_TOKEN])
            except EonNextAuthError:
                errors["base"] = "invalid_auth"
            except NoPrepayMeters:
                errors["base"] = "no_prepay_meters"
            except EonNextApiError:
                _LOGGER.exception("Error talking to E.ON Next")
                errors["base"] = "cannot_connect"
        return self.async_show_form(step_id=step_id, data_schema=TOKEN_SCHEMA, errors=errors)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up from a refresh token copied from the E.ON Next website."""
        result = await self._async_handle_token("user", user_input)
        if not isinstance(result, tuple):
            return result
        account_number, refresh_token = result
        await self.async_set_unique_id(account_number)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"E.ON Next {account_number}",
            data={CONF_REFRESH_TOKEN: refresh_token},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The refresh token chain broke - ask for a new one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_update_token("reauth_confirm", user_input, self._get_reauth_entry())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste a fresh refresh token at any time."""
        return await self._async_update_token(
            "reconfigure", user_input, self._get_reconfigure_entry()
        )

    async def _async_update_token(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        entry: EonNextPrepayConfigEntry,
    ) -> ConfigFlowResult:
        result = await self._async_handle_token(step_id, user_input)
        if not isinstance(result, tuple):
            return result
        account_number, refresh_token = result
        await self.async_set_unique_id(account_number)
        self._abort_if_unique_id_mismatch(reason="wrong_account")
        return self.async_update_reload_and_abort(
            entry, data_updates={CONF_REFRESH_TOKEN: refresh_token}
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: EonNextPrepayConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """Low-credit threshold and polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LOW_CREDIT_THRESHOLD,
                    default=options.get(CONF_LOW_CREDIT_THRESHOLD, DEFAULT_LOW_CREDIT_THRESHOLD),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=100, step=0.5, unit_of_measurement="£",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL, max=240, step=5, unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
