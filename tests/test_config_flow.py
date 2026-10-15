"""Tests for the E.ON Next Prepay config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eon_next_prepay.api import EonNextApiError, EonNextAuthError
from custom_components.eon_next_prepay.const import (
    CONF_LOW_CREDIT_THRESHOLD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import ACCOUNT, accounts_payload

CLIENT = "custom_components.eon_next_prepay.config_flow.EonNextClient"


def mock_client(accounts=None, side_effect=None, rotated="rt-rotated"):
    """Patch the flow's client: returns accounts and reports a rotated token."""
    patcher = patch(CLIENT, autospec=True)
    client_cls = patcher.start()
    client = client_cls.return_value
    client.async_get_accounts.return_value = accounts
    client.async_get_accounts.side_effect = side_effect
    client.refresh_token = rotated
    return patcher


async def test_user_flow_stores_rotated_token(hass: HomeAssistant) -> None:
    patcher = mock_client(accounts_payload())
    with patch("custom_components.eon_next_prepay.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: " rt-pasted "}
        )
    patcher.stop()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"E.ON Next {ACCOUNT}"
    # the pasted token was spent validating, so the rotated one must be kept
    assert result["data"] == {CONF_REFRESH_TOKEN: "rt-rotated"}
    assert result["result"].unique_id == ACCOUNT


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    for side_effect, accounts, error in (
        (EonNextAuthError("dead"), None, "invalid_auth"),
        (EonNextApiError("down"), None, "cannot_connect"),
        (None, [{"number": ACCOUNT, "properties": []}], "no_prepay_meters"),
    ):
        patcher = mock_client(accounts, side_effect)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: "rt"}
        )
        patcher.stop()
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}


async def test_reauth_updates_token(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    patcher = mock_client(accounts_payload())
    with patch("custom_components.eon_next_prepay.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REFRESH_TOKEN: "new"}
        )
    patcher.stop()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "rt-rotated"


async def test_reconfigure_rejects_other_account(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="A-OTHER", data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)

    patcher = mock_client(accounts_payload())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "new"}
    )
    patcher.stop()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "old"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, data={CONF_REFRESH_TOKEN: "rt"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOW_CREDIT_THRESHOLD: 10, CONF_SCAN_INTERVAL: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_LOW_CREDIT_THRESHOLD: 10, CONF_SCAN_INTERVAL: 15}
