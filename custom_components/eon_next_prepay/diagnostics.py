"""Diagnostics for E.ON Next Prepay."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_REFRESH_TOKEN
from .coordinator import EonNextPrepayConfigEntry

TO_REDACT = {CONF_API_KEY, CONF_REFRESH_TOKEN, "account_number", "mpxn", "serial", "device_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EonNextPrepayConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    meters = list(coordinator.meters.values())
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "meters": [
            {
                **async_redact_data(asdict(meter), TO_REDACT),
                "telemetry_supported": meter.device_id in coordinator.telemetry_supported,
                "data": asdict(coordinator.data.meters[meter.device_id])
                if meter.device_id in coordinator.data.meters
                else None,
            }
            for meter in meters
        ],
    }
