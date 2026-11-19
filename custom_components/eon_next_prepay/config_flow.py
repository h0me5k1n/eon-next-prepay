"""Config flow for E.ON Next Prepay."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowHandler, FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    EonNextApiError,
    EonNextApiKeyExists,
    EonNextAuthError,
    EonNextClient,
    looks_like_api_key,
)
from .const import (
    CONF_API_KEY,
    CONF_CREATE_API_KEY,
    CONF_LOW_CREDIT_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_LOW_CREDIT_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import EonNextPrepayConfigEntry, find_prepay_meters

_LOGGER = logging.getLogger(__name__)

SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): SECRET,
        vol.Optional(CONF_CREATE_API_KEY, default=True): BooleanSelector(),
    }
)
API_KEY_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): SECRET})


class NoPrepayMeters(Exception):
    """The account has no smart meters in prepayment mode."""


async def validate_client(client: EonNextClient) -> str:
    """Check the client can log in and the account has prepay meters.

    Returns the account number. A refresh-token client spends its token doing
    this, so read ``client.refresh_token`` afterwards for the usable one.
    """
    meters, _ = find_prepay_meters(await client.async_get_accounts())
    if not meters:
        raise NoPrepayMeters
    return meters[0].account_number


def api_key_errors(err: Exception) -> dict[str, str]:
    """Form errors for a failed login or key creation."""
    if isinstance(err, EonNextAuthError):
        return {"base": "invalid_auth"}
    if isinstance(err, NoPrepayMeters):
        return {"base": "no_prepay_meters"}
    _LOGGER.error("Error talking to E.ON Next: %s", err)
    return {"base": "cannot_connect"}


class ApiKeySteps(FlowHandler):
    """Steps for getting an API key, shared by the config and repair flows.

    The subclass sets ``_client`` to a logged-in refresh-token client before
    calling ``_async_create_api_key``, and implements the two finish methods.
    """

    _client: EonNextClient
    _account_number: str
    _api_key: str | None = None

    async def _async_finish_api_key(self, api_key: str) -> FlowResult:
        raise NotImplementedError

    async def _async_finish_refresh_token(self) -> FlowResult:
        raise NotImplementedError

    async def _async_key_created(self, api_key: str) -> None:
        """Save a new key straight away where possible.

        E.ON shows a key only once, so if the user closes the flow on the
        "key created" step, the key mustn't be lost.
        """

    async def _async_create_failed(self) -> FlowResult:
        """Creating a key failed, but the login still works - keep the token."""
        return await self._async_finish_refresh_token()

    def _check_account(self, account_number: str) -> None:
        """Abort if a pasted key belongs to a different account."""
        if account_number != self._account_number:
            raise AbortFlow("wrong_account")

    async def _async_create_api_key(self, replace: bool = False) -> FlowResult:
        try:
            self._api_key = await self._client.async_create_api_key(replace=replace)
        except EonNextApiKeyExists as err:
            return await self.async_step_api_key_exists(masked=err.masked)
        except (EonNextAuthError, EonNextApiError) as err:
            _LOGGER.warning("Couldn't create an E.ON Next API key: %s", err)
            return await self._async_create_failed()
        await self._async_key_created(self._api_key)
        return await self.async_step_api_key_created()

    async def async_step_api_key_exists(
        self, user_input: dict[str, Any] | None = None, masked: str = ""
    ) -> FlowResult:
        """The account already has a key: paste it, replace it, or keep the token."""
        return self.async_show_menu(
            step_id="api_key_exists",
            menu_options=["enter_api_key", "replace_api_key", "keep_refresh_token"],
            description_placeholders={"masked": masked or "?"},
        )

    async def async_step_enter_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Paste the account's existing API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            if not looks_like_api_key(api_key):
                errors["base"] = "not_an_api_key"
            else:
                client = EonNextClient(async_get_clientsession(self.hass), api_key=api_key)
                try:
                    account_number = await validate_client(client)
                except (EonNextAuthError, EonNextApiError, NoPrepayMeters) as err:
                    errors = api_key_errors(err)
                else:
                    self._check_account(account_number)
                    return await self._async_finish_api_key(api_key)
        return self.async_show_form(
            step_id="enter_api_key", data_schema=API_KEY_SCHEMA, errors=errors
        )

    async def async_step_replace_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm replacing the existing key - anything else using it stops working."""
        if user_input is not None:
            return await self._async_create_api_key(replace=True)
        return self.async_show_form(step_id="replace_api_key", data_schema=vol.Schema({}))

    async def async_step_keep_refresh_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Carry on with the refresh token, which will need replacing in about a week."""
        return await self._async_finish_refresh_token()

    async def async_step_api_key_created(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the new key once, so the user can save it for a reinstall."""
        assert self._api_key is not None
        if user_input is not None:
            return await self._async_finish_api_key(self._api_key)
        return self.async_show_form(
            step_id="api_key_created",
            data_schema=vol.Schema({}),
            description_placeholders={"api_key": self._api_key},
        )


class EonNextPrepayConfigFlow(ApiKeySteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for E.ON Next Prepay.

    Entry data holds either ``api_key`` or ``refresh_token``, never both.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up from an API key, or a refresh token copied from eonnext.com."""
        return await self._async_step_token("user", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The saved login stopped working - ask for a new one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_token("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the login at any time."""
        return await self._async_step_token("reconfigure", user_input)

    async def _async_step_token(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Log in with whatever was pasted, then get an API key if asked to."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            if looks_like_api_key(token):
                client = EonNextClient(session, api_key=token)
            else:
                client = EonNextClient(session, token)
            try:
                account_number = await validate_client(client)
            except (EonNextAuthError, EonNextApiError, NoPrepayMeters) as err:
                errors = api_key_errors(err)
            else:
                # check the account before touching its API key
                await self._async_check_unique_id(account_number)
                self._client = client
                self._account_number = account_number
                if client.uses_api_key:
                    return await self._async_finish_api_key(token)
                if user_input.get(CONF_CREATE_API_KEY, True):
                    return await self._async_create_api_key()
                return await self._async_finish_refresh_token()
        return self.async_show_form(step_id=step_id, data_schema=TOKEN_SCHEMA, errors=errors)

    async def _async_check_unique_id(self, account_number: str) -> None:
        await self.async_set_unique_id(account_number)
        if self.source == SOURCE_USER:
            self._abort_if_unique_id_configured()
        else:
            self._abort_if_unique_id_mismatch(reason="wrong_account")

    async def _async_finish(self, data: dict[str, str]) -> ConfigFlowResult:
        if self.source == SOURCE_USER:
            return self.async_create_entry(title=f"E.ON Next {self._account_number}", data=data)
        entry = (
            self._get_reauth_entry()
            if self.source == "reauth"
            else self._get_reconfigure_entry()
        )
        # replace the data outright, so the old credential doesn't linger
        return self.async_update_reload_and_abort(entry, data=data)

    async def _async_finish_api_key(self, api_key: str) -> ConfigFlowResult:
        return await self._async_finish({CONF_API_KEY: api_key})

    async def _async_key_created(self, api_key: str) -> None:
        # a new setup has no entry yet; reauth and reconfigure do
        if self.source != SOURCE_USER:
            entry = (
                self._get_reauth_entry()
                if self.source == "reauth"
                else self._get_reconfigure_entry()
            )
            self.hass.config_entries.async_update_entry(entry, data={CONF_API_KEY: api_key})

    async def _async_finish_refresh_token(self) -> ConfigFlowResult:
        # the pasted token was spent logging in; only the rotated one works
        assert self._client.refresh_token is not None
        return await self._async_finish({CONF_REFRESH_TOKEN: self._client.refresh_token})

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
