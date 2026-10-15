"""Low-credit binary sensor for E.ON Next Prepay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_LOW_CREDIT_THRESHOLD, DEFAULT_LOW_CREDIT_THRESHOLD
from .coordinator import EonNextPrepayConfigEntry, EonNextPrepayCoordinator, PrepayMeter
from .entity import EonNextPrepayEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EonNextPrepayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    threshold = float(
        entry.options.get(CONF_LOW_CREDIT_THRESHOLD, DEFAULT_LOW_CREDIT_THRESHOLD)
    )
    async_add_entities(
        LowCreditSensor(coordinator, meter, threshold)
        for meter in coordinator.meters.values()
    )


class LowCreditSensor(EonNextPrepayEntity, BinarySensorEntity):
    """On when credit drops below the configured threshold."""

    _attr_icon = "mdi:cash-alert"

    def __init__(
        self, coordinator: EonNextPrepayCoordinator, meter: PrepayMeter, threshold: float
    ) -> None:
        super().__init__(coordinator, meter, "low_credit")
        self._threshold = threshold

    @property
    def is_on(self) -> bool | None:
        data = self.meter_data
        credit = (data.balance or {}).get("creditInPence") if data else None
        if credit is None:
            return None
        return credit / 100 < self._threshold

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"threshold": self._threshold}
