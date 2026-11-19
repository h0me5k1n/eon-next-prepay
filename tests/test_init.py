"""Tests for setup, the coordinator and entities."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.eon_next_prepay.api import (
    EonNextApiError,
    EonNextApiKeyExists,
    EonNextAuthError,
    EonNextClient,
)
from custom_components.eon_next_prepay.const import CONF_API_KEY, CONF_REFRESH_TOKEN, DOMAIN

from .conftest import ACCOUNT, ELEC_DEVICE, GAS_DEVICE, accounts_payload

BALANCE = {
    "asAt": "2026-10-15T11:30:00+00:00",
    "creditInPence": 412,
    "debtInPence": 1250,
    "emergencyCreditInPence": 0,
}
PAYMENTS = [
    {"amount": 1000, "paymentDate": "2026-10-01", "smartPrepayProcessStatus": "COMPLETED", "meter": {"serialNumber": "G1"}},
    {"amount": 2000, "paymentDate": "2026-10-10", "smartPrepayProcessStatus": "COMPLETED", "meter": {"serialNumber": "G1"}},
    {"amount": 5000, "paymentDate": "2026-10-12", "smartPrepayProcessStatus": "COMPLETED", "meter": {"serialNumber": "E1"}},
]
CONSUMPTION = [
    {"value": "2.1", "startAt": "2026-10-13T00:00:00+01:00"},
    {"value": "3.4", "startAt": "2026-10-14T00:00:00+01:00"},
    {"value": "0.4", "startAt": "2026-10-15T00:00:00+01:00"},
]


class FakeApi:
    """Stands in for the network side of EonNextClient."""

    def __init__(self) -> None:
        self.balance_error: Exception | None = None
        self.telemetry_error: Exception | None = EonNextApiError("no CAD", {"KT-GB-4056"})
        self.telemetry_calls = 0
        self.rotate_to: str | None = None
        self.accounts_error: Exception | None = None
        self.consumption_error: Exception | None = None
        self.existing_key: str | None = None
        self.created_keys: list[bool] = []
        self.clients: list[EonNextClient] = []
        self.auto_topups: list[dict] = [
            {"topupAmount": 1500, "effectiveFrom": "2026-01-01T00:00:00Z", "effectiveTo": "2026-02-01T00:00:00Z"},
            {"topupAmount": 2500, "effectiveFrom": "2026-02-01T00:00:00Z", "effectiveTo": None},
        ]

    def install(self):
        api = self

        async def get_accounts(client: EonNextClient):
            if client not in api.clients:
                api.clients.append(client)
            if api.accounts_error:
                raise api.accounts_error
            if api.rotate_to and client._on_refresh_token:
                client._on_refresh_token(api.rotate_to)
            return accounts_payload()

        async def get_balance(client, device_id):
            assert device_id == GAS_DEVICE
            if api.balance_error:
                raise api.balance_error
            return BALANCE

        async def get_payments(client, account_number):
            assert account_number == ACCOUNT
            return PAYMENTS

        async def get_auto_topup(client, account_number, device_id):
            return api.auto_topups

        async def get_telemetry(client, device_id, start, end):
            api.telemetry_calls += 1
            if api.telemetry_error:
                raise api.telemetry_error
            return [
                {"readAt": start.isoformat(), "consumptionDelta": "1200", "costDeltaWithTax": "15.5"},
                {"readAt": end.isoformat(), "consumptionDelta": "800", "costDeltaWithTax": "10.25"},
            ]

        async def get_consumption(client, account_number, start_at, timezone):
            assert account_number == ACCOUNT
            if api.consumption_error:
                raise api.consumption_error
            return {"G1": CONSUMPTION, "E1": []}

        async def create_api_key(client, replace=False):
            api.created_keys.append(replace)
            if api.existing_key and not replace:
                raise EonNextApiKeyExists(api.existing_key)
            return "sk_live_new"

        target = "custom_components.eon_next_prepay.api.EonNextClient"
        return [
            patch(f"{target}.async_create_api_key", create_api_key),
            patch(f"{target}.async_get_accounts", get_accounts),
            patch(f"{target}.async_get_balance", get_balance),
            patch(f"{target}.async_get_payments", get_payments),
            patch(f"{target}.async_get_auto_topup_configs", get_auto_topup),
            patch(f"{target}.async_get_telemetry", get_telemetry),
            patch(f"{target}.async_get_consumption", get_consumption),
        ]


@pytest.fixture
def api():
    fake = FakeApi()
    patchers = fake.install()
    for p in patchers:
        p.start()
    yield fake
    for p in patchers:
        p.stop()


async def setup_entry(
    hass: HomeAssistant, options: dict | None = None, data: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data=data or {CONF_REFRESH_TOKEN: "rt-1"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def frozen_now(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(datetime(2026, 10, 15, 12, 0, tzinfo=dt_util.get_time_zone("Europe/London")))
    return freezer


async def test_setup_creates_prepay_entities_only(
    hass: HomeAssistant, api: FakeApi, frozen_now
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED

    def state(object_id: str) -> str:
        return hass.states.get(f"{object_id}").state

    assert state("sensor.e_on_next_gas_prepay_credit") == "4.12"
    assert state("sensor.e_on_next_gas_prepay_debt") == "12.5"
    assert state("sensor.e_on_next_gas_prepay_emergency_credit") == "0.0"
    assert state("sensor.e_on_next_gas_prepay_weekly_debt_recovery") == "3.5"
    # latest gas payment, not the newer electricity one
    assert state("sensor.e_on_next_gas_prepay_last_top_up") == "20.0"
    assert state("sensor.e_on_next_gas_prepay_last_top_up_date") == "2026-10-10"
    # the expired config is skipped
    assert state("sensor.e_on_next_gas_prepay_auto_top_up_amount") == "25.0"
    usage = hass.states.get("sensor.e_on_next_gas_prepay_usage_last_full_day")
    assert usage.state == "3.4"
    assert usage.attributes["unit_of_measurement"] == "m³"
    assert usage.attributes["date"] == datetime(2026, 10, 14).date()
    # £4.12 is below the default £5 threshold
    assert state("binary_sensor.e_on_next_gas_prepay_low_credit") == "on"

    # no telemetry sensors without a consumer access device, and no credit-mode meter
    assert hass.states.get("sensor.e_on_next_gas_prepay_usage_today") is None
    assert not [s for s in hass.states.async_entity_ids() if "electricity" in s]
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.identifiers for d in devices] == [{(DOMAIN, GAS_DEVICE)}]
    assert devices[0].serial_number == "G1"
    assert ELEC_DEVICE not in str(devices)


async def test_unsupported_telemetry_is_not_retried(
    hass: HomeAssistant, api: FakeApi, frozen_now
) -> None:
    entry = await setup_entry(hass)
    await entry.runtime_data.async_refresh()
    assert api.telemetry_calls == 1


async def test_telemetry_sensors(hass: HomeAssistant, api: FakeApi, frozen_now) -> None:
    api.telemetry_error = None
    await setup_entry(hass)
    usage = hass.states.get("sensor.e_on_next_gas_prepay_usage_today")
    cost = hass.states.get("sensor.e_on_next_gas_prepay_cost_today")
    assert usage.state == "2.0"
    assert usage.attributes["state_class"] == "total_increasing"
    assert cost.state == "0.26"
    assert cost.attributes["state_class"] == "total"


async def test_low_credit_threshold_option(hass: HomeAssistant, api: FakeApi) -> None:
    await setup_entry(hass, {"low_credit_threshold": 2})
    assert hass.states.get("binary_sensor.e_on_next_gas_prepay_low_credit").state == "off"


async def test_missing_balance_snapshot_is_unknown(hass: HomeAssistant, api: FakeApi) -> None:
    api.balance_error = EonNextApiError("not found", {"KT-GB-4049"})
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.e_on_next_gas_prepay_credit").state == "unknown"
    assert hass.states.get("binary_sensor.e_on_next_gas_prepay_low_credit").state == "unknown"


async def test_rotated_token_is_saved(hass: HomeAssistant, api: FakeApi) -> None:
    api.rotate_to = "rt-2"
    entry = await setup_entry(hass)
    assert entry.data[CONF_REFRESH_TOKEN] == "rt-2"
    # saving the token must not trigger a reload
    assert entry.state is ConfigEntryState.LOADED


async def test_dead_token_starts_reauth(hass: HomeAssistant, api: FakeApi) -> None:
    api.accounts_error = EonNextAuthError("Unknown or invalid refresh token.")
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_api_outage_retries_setup(hass: HomeAssistant, api: FakeApi) -> None:
    api.accounts_error = EonNextApiError("down")
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_outage_after_setup_marks_unavailable(
    hass: HomeAssistant, api: FakeApi, frozen_now
) -> None:
    await setup_entry(hass)
    api.accounts_error = EonNextApiError("down")
    frozen_now.tick(31 * 60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.e_on_next_gas_prepay_credit").state == "unavailable"


async def test_unused_features_create_no_sensors(hass: HomeAssistant, api: FakeApi) -> None:
    api.auto_topups = []
    await setup_entry(hass)
    assert hass.states.get("sensor.e_on_next_gas_prepay_auto_top_up_amount") is None
    assert hass.states.get("sensor.e_on_next_gas_prepay_credit") is not None


async def test_consumption_failure_keeps_last_value(
    hass: HomeAssistant, api: FakeApi, frozen_now
) -> None:
    await hass.config.async_set_time_zone("Europe/London")
    entry = await setup_entry(hass)
    api.consumption_error = EonNextApiError("busy")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.e_on_next_gas_prepay_usage_last_full_day").state == "3.4"
    assert hass.states.get("sensor.e_on_next_gas_prepay_credit").state == "4.12"


def test_partial_newest_day_is_ignored(hass: HomeAssistant, frozen_now) -> None:
    from custom_components.eon_next_prepay.coordinator import MeterData, latest_complete_day

    # electricity on the live account: 10-14 holds only 00:00-01:00, and buckets arrive unordered
    buckets = [
        {"value": "0.410", "startAt": "2026-10-14T00:00:00+01:00"},
        {"value": "13.345", "startAt": "2026-10-12T00:00:00+01:00"},
        {"value": "12.186", "startAt": "2026-10-13T00:00:00+01:00"},
    ]
    data = MeterData()
    latest_complete_day(buckets, data)
    assert (str(data.last_day), data.last_day_consumption) == ("2026-10-13", 12.186)

    data = MeterData()
    latest_complete_day(buckets[:1], data)
    assert data.last_day is None


def switch_issue(hass: HomeAssistant, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"switch_to_api_key_{entry.entry_id}")


async def test_api_key_entry_uses_key_and_has_no_issue(hass: HomeAssistant, api: FakeApi) -> None:
    entry = await setup_entry(hass, data={CONF_API_KEY: "sk_live_x"})
    assert entry.state is ConfigEntryState.LOADED
    assert api.clients[-1].uses_api_key
    assert switch_issue(hass, entry) is None


async def test_refresh_token_entry_offers_switch_without_changing_anything(
    hass: HomeAssistant, api: FakeApi
) -> None:
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert not api.clients[-1].uses_api_key
    issue = switch_issue(hass, entry)
    assert issue is not None and issue.is_fixable
    # opt-in: no key is created just by updating
    assert api.created_keys == []
    assert entry.data == {CONF_REFRESH_TOKEN: "rt-1"}


async def test_rotation_after_switch_does_not_restore_refresh_token(
    hass: HomeAssistant, api: FakeApi
) -> None:
    entry = await setup_entry(hass)
    hass.config_entries.async_update_entry(entry, data={CONF_API_KEY: "sk_live_new"})
    api.clients[-1]._on_refresh_token("rt-late")
    assert entry.data == {CONF_API_KEY: "sk_live_new"}


async def start_repair(hass: HomeAssistant, entry: MockConfigEntry) -> tuple:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    result = await manager.async_init(
        DOMAIN, context={"issue_id": f"switch_to_api_key_{entry.entry_id}"}
    )
    return manager, result


async def test_repair_switches_to_api_key(hass: HomeAssistant, api: FakeApi) -> None:
    entry = await setup_entry(hass)
    manager, result = await start_repair(hass, entry)
    assert result["step_id"] == "confirm"
    assert api.created_keys == []

    result = await manager.async_configure(result["flow_id"], {})
    assert result["step_id"] == "api_key_created"
    assert result["description_placeholders"] == {"api_key": "sk_live_new"}
    # saved before it's shown, in case the dialog is closed
    assert entry.data == {CONF_API_KEY: "sk_live_new"}

    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert api.clients[-1].uses_api_key
    assert switch_issue(hass, entry) is None


async def test_repair_with_existing_key_changes_nothing_unless_asked(
    hass: HomeAssistant, api: FakeApi
) -> None:
    api.existing_key = "sk_live_****abcd"
    entry = await setup_entry(hass)
    manager, result = await start_repair(hass, entry)
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"] == {"masked": "sk_live_****abcd"}

    result = await manager.async_configure(
        result["flow_id"], {"next_step_id": "keep_refresh_token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "kept_refresh_token"
    assert api.created_keys == [False]
    assert entry.data == {CONF_REFRESH_TOKEN: "rt-1"}
    assert switch_issue(hass, entry) is not None


async def test_repair_needs_a_running_entry(hass: HomeAssistant, api: FakeApi) -> None:
    entry = await setup_entry(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    _, result = await start_repair(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"
