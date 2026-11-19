"""Tests for the E.ON Next Prepay config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eon_next_prepay.api import (
    EonNextApiError,
    EonNextApiKeyExists,
    EonNextAuthError,
    EonNextClient,
)
from custom_components.eon_next_prepay.const import (
    CONF_API_KEY,
    CONF_CREATE_API_KEY,
    CONF_LOW_CREDIT_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DOMAIN,
)

from .conftest import ACCOUNT, accounts_payload

GOOD_KEY = "sk_live_existing"
NEW_KEY = "sk_live_new"


class FakeKraken:
    """Stands in for the network side of EonNextClient, using its real auth state."""

    def __init__(self) -> None:
        self.api_keys = {GOOD_KEY}
        self.accounts: list[dict] | None = None
        self.accounts_error: Exception | None = None
        self.existing_key: str | None = None
        self.create_error: Exception | None = None
        self.create_calls: list[bool] = []

    def install(self):
        fake = self

        async def get_accounts(client: EonNextClient):
            if fake.accounts_error:
                raise fake.accounts_error
            if client.uses_api_key:
                if client._api_key not in fake.api_keys:
                    raise EonNextAuthError("E.ON Next rejected the API key")
            else:
                # logging in spends the pasted refresh token
                client._refresh_token = "rt-rotated"
            return fake.accounts if fake.accounts is not None else accounts_payload()

        async def create_api_key(client: EonNextClient, replace: bool = False):
            assert not client.uses_api_key
            fake.create_calls.append(replace)
            if fake.create_error:
                raise fake.create_error
            if fake.existing_key and not replace:
                raise EonNextApiKeyExists(fake.existing_key)
            fake.api_keys = {NEW_KEY}
            return NEW_KEY

        return patch.multiple(
            EonNextClient, async_get_accounts=get_accounts, async_create_api_key=create_api_key
        )


@pytest.fixture
def kraken():
    fake = FakeKraken()
    with fake.install(), patch(
        "custom_components.eon_next_prepay.async_setup_entry", return_value=True
    ):
        yield fake


async def start(hass: HomeAssistant, token: str, **extra) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: token, **extra}
    )


async def test_api_key_is_used_directly(hass: HomeAssistant, kraken: FakeKraken) -> None:
    result = await start(hass, f" {GOOD_KEY} ")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"E.ON Next {ACCOUNT}"
    assert result["data"] == {CONF_API_KEY: GOOD_KEY}
    assert result["result"].unique_id == ACCOUNT
    assert kraken.create_calls == []


async def test_refresh_token_creates_and_shows_key(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    result = await start(hass, "rt-pasted")
    assert result["step_id"] == "api_key_created"
    assert result["description_placeholders"] == {"api_key": NEW_KEY}
    assert kraken.create_calls == [False]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: NEW_KEY}


async def test_refresh_token_without_key(hass: HomeAssistant, kraken: FakeKraken) -> None:
    result = await start(hass, "rt-pasted", **{CONF_CREATE_API_KEY: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # the pasted token was spent validating, so the rotated one must be kept
    assert result["data"] == {CONF_REFRESH_TOKEN: "rt-rotated"}
    assert kraken.create_calls == []


async def test_existing_key_can_be_entered(hass: HomeAssistant, kraken: FakeKraken) -> None:
    kraken.existing_key = "sk_live_****ting"
    result = await start(hass, "rt-pasted")
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "api_key_exists"
    assert result["description_placeholders"] == {"masked": "sk_live_****ting"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "enter_api_key"}
    )
    for bad, error in (("rt-oops", "not_an_api_key"), ("sk_live_wrong", "invalid_auth")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_KEY: bad}
        )
        assert result["errors"] == {"base": error}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: GOOD_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_API_KEY: GOOD_KEY}
    # never replaced the key behind the user's back
    assert kraken.create_calls == [False]


async def test_existing_key_can_be_replaced(hass: HomeAssistant, kraken: FakeKraken) -> None:
    kraken.existing_key = "sk_live_****ting"
    result = await start(hass, "rt-pasted")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "replace_api_key"}
    )
    assert result["step_id"] == "replace_api_key"
    assert kraken.create_calls == [False]  # nothing replaced until confirmed
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "api_key_created"
    assert kraken.create_calls == [False, True]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"] == {CONF_API_KEY: NEW_KEY}


async def test_existing_key_keep_refresh_token(hass: HomeAssistant, kraken: FakeKraken) -> None:
    kraken.existing_key = "sk_live_****ting"
    result = await start(hass, "rt-pasted")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "keep_refresh_token"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REFRESH_TOKEN: "rt-rotated"}


async def test_failed_key_creation_keeps_refresh_token(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    kraken.create_error = EonNextApiError("nope")
    result = await start(hass, "rt-pasted")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_REFRESH_TOKEN: "rt-rotated"}


async def test_user_flow_errors(hass: HomeAssistant, kraken: FakeKraken) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    for accounts_error, accounts, error in (
        (EonNextAuthError("dead"), None, "invalid_auth"),
        (EonNextApiError("down"), None, "cannot_connect"),
        (None, [{"number": ACCOUNT, "properties": []}], "no_prepay_meters"),
    ):
        kraken.accounts_error, kraken.accounts = accounts_error, accounts
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "rt"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
    assert kraken.create_calls == []


async def test_already_configured_aborts_before_creating_key(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_API_KEY: GOOD_KEY}).add_to_hass(hass)
    result = await start(hass, "rt-pasted")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert kraken.create_calls == []


async def test_reauth_saves_new_key_before_it_is_shown(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "new"}
    )
    assert result["step_id"] == "api_key_created"
    # E.ON shows a key once - closing the dialog here mustn't lose it
    assert entry.data == {CONF_API_KEY: NEW_KEY}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_API_KEY: NEW_KEY}


async def test_reauth_with_api_key_drops_refresh_token(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: GOOD_KEY}
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data == {CONF_API_KEY: GOOD_KEY}


async def test_reconfigure_rejects_other_account(
    hass: HomeAssistant, kraken: FakeKraken
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="A-OTHER", data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data == {CONF_REFRESH_TOKEN: "old"}
    assert kraken.create_calls == []


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_REFRESH_TOKEN: "rt"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOW_CREDIT_THRESHOLD: 10, CONF_SCAN_INTERVAL: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_LOW_CREDIT_THRESHOLD: 10, CONF_SCAN_INTERVAL: 15}
