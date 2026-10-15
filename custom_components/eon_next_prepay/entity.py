"""Base entity for E.ON Next Prepay."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, FUEL_GAS
from .coordinator import EonNextPrepayCoordinator, MeterData, PrepayMeter


class EonNextPrepayEntity(CoordinatorEntity[EonNextPrepayCoordinator]):
    """An entity belonging to one prepay meter."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EonNextPrepayCoordinator, meter: PrepayMeter, key: str
    ) -> None:
        super().__init__(coordinator)
        self._device_id = meter.device_id
        self._attr_unique_id = f"{meter.device_id}_{key}"
        self._attr_translation_key = key
        fuel = "Gas" if meter.fuel == FUEL_GAS else "Electricity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter.device_id)},
            name=f"E.ON Next {fuel} Prepay",
            manufacturer="E.ON Next",
            model=f"Smart prepay {fuel.lower()} meter",
            serial_number=meter.serial or None,
        )

    @property
    def meter_data(self) -> MeterData | None:
        return self.coordinator.data.meters.get(self._device_id)

    @property
    def available(self) -> bool:
        return super().available and self.meter_data is not None
