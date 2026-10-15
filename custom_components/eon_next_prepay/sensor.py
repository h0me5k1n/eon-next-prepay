"""Sensors for E.ON Next Prepay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import (
    EonNextPrepayConfigEntry,
    EonNextPrepayCoordinator,
    MeterData,
    PrepayMeter,
    parse_date,
    pence_to_pounds,
)
from .entity import EonNextPrepayEntity

CURRENCY = "GBP"


def _balance(field: str) -> Callable[[MeterData], Any]:
    return lambda d: pence_to_pounds((d.balance or {}).get(field))


def _payment(field: str) -> Callable[[MeterData], Any]:
    return lambda d: (d.last_payment or {}).get(field)


@dataclass(frozen=True, kw_only=True)
class PrepaySensorDescription(SensorEntityDescription):
    """Describes a prepay sensor."""

    value_fn: Callable[[MeterData], Any]
    attrs_fn: Callable[[MeterData], dict[str, Any]] | None = None
    telemetry: bool = False
    # only created if this has a value at setup (e.g. no auto top-up configured)
    optional: bool = False


MONEY = {
    "device_class": SensorDeviceClass.MONETARY,
    "native_unit_of_measurement": CURRENCY,
    "suggested_display_precision": 2,
}

SENSORS: tuple[PrepaySensorDescription, ...] = (
    PrepaySensorDescription(
        key="credit", value_fn=_balance("creditInPence"), **MONEY,
    ),
    PrepaySensorDescription(
        key="debt", value_fn=_balance("debtInPence"), **MONEY,
    ),
    PrepaySensorDescription(
        key="emergency_credit", value_fn=_balance("emergencyCreditInPence"), **MONEY,
    ),
    PrepaySensorDescription(
        key="balance_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: dt_util.parse_datetime((d.balance or {}).get("asAt") or ""),
    ),
    PrepaySensorDescription(
        key="weekly_debt_recovery",
        value_fn=lambda d: pence_to_pounds(d.weekly_debt_recovery_pence),
        optional=True,
        **MONEY,
    ),
    PrepaySensorDescription(
        key="last_top_up",
        value_fn=lambda d: pence_to_pounds(_payment("amount")(d)),
        attrs_fn=lambda d: {
            "date": _payment("paymentDate")(d),
            "status": _payment("smartPrepayProcessStatus")(d),
        },
        **MONEY,
    ),
    PrepaySensorDescription(
        key="last_top_up_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda d: parse_date(_payment("paymentDate")(d)),
    ),
    PrepaySensorDescription(
        key="auto_top_up",
        value_fn=lambda d: pence_to_pounds(d.auto_topup_pence),
        optional=True,
        **MONEY,
    ),
    PrepaySensorDescription(
        key="usage_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda d: d.today_consumption_kwh,
        telemetry=True,
    ),
    PrepaySensorDescription(
        key="cost_today",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d: d.today_cost,
        telemetry=True,
        **MONEY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EonNextPrepayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for meter in coordinator.meters.values():
        data = coordinator.data.meters.get(meter.device_id)
        entities.extend(
            PrepaySensor(coordinator, meter, description)
            for description in SENSORS
            if (not description.telemetry or meter.device_id in coordinator.telemetry_supported)
            and (not description.optional or (data and description.value_fn(data) is not None))
        )
        entities.append(DailyUsageSensor(coordinator, meter))
    async_add_entities(entities)


class PrepaySensor(EonNextPrepayEntity, SensorEntity):
    """A value read from the coordinator for one meter."""

    entity_description: PrepaySensorDescription

    def __init__(
        self,
        coordinator: EonNextPrepayCoordinator,
        meter: PrepayMeter,
        description: PrepaySensorDescription,
    ) -> None:
        super().__init__(coordinator, meter, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.meter_data
        return self.entity_description.value_fn(data) if data else None

    @property
    def last_reset(self) -> datetime | None:
        # cost_today restarts at local midnight
        if self.entity_description.state_class == SensorStateClass.TOTAL:
            return dt_util.start_of_local_day()
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.meter_data
        if data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(data)


class DailyUsageSensor(EonNextPrepayEntity, SensorEntity):
    """Consumption for the latest complete day the smart meter has reported.

    Units follow the meter: kWh, or m³ for SMETS2 gas meters.
    """

    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EonNextPrepayCoordinator, meter: PrepayMeter) -> None:
        super().__init__(coordinator, meter, "usage_last_day")
        units = (meter.consumption_units or "").lower().replace("^3", "³")
        if units in ("m³", "m3", "cubic metres", "cubic meters"):
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    @property
    def native_value(self) -> float | None:
        data = self.meter_data
        return data.last_day_consumption if data else None

    @property
    def extra_state_attributes(self) -> dict[str, date | None] | None:
        data = self.meter_data
        return {"date": data.last_day} if data else None
