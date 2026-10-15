"""Data coordinator for the E.ON Next Prepay integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EonNextApiError, EonNextAuthError, EonNextClient
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    FUEL_ELECTRICITY,
    FUEL_GAS,
)

_LOGGER = logging.getLogger(__name__)

# smart meter types that hold the prepay balance (electricity / gas)
METER_DEVICE_TYPES = {"ESME", "GSME"}
# telemetry errors meaning there's no consumer access device - stop asking
TELEMETRY_UNSUPPORTED_CODES = {"KT-GB-4039", "KT-GB-4050", "KT-GB-4056"}
# device not in prepay mode / no snapshot yet - show unknown rather than fail
BALANCE_MISSING_CODES = {"KT-GB-4048", "KT-GB-4049"}
# daily consumption lags by a day or more, so look back a week for the latest
CONSUMPTION_LOOKBACK_DAYS = 7
# E.ON Next only supplies GB meters; days are UK days whatever HA's timezone is
SUPPLY_TIMEZONE = "Europe/London"
UK = dt_util.get_time_zone(SUPPLY_TIMEZONE)

type EonNextPrepayConfigEntry = ConfigEntry[EonNextPrepayCoordinator]


@dataclass(frozen=True)
class PrepayMeter:
    """A smart meter in prepayment mode."""

    device_id: str
    fuel: str
    account_number: str
    mpxn: str
    serial: str
    consumption_units: str | None


@dataclass
class MeterData:
    """Latest values for one prepay meter."""

    balance: dict | None = None
    weekly_debt_recovery_pence: int | None = None
    auto_topup_pence: int | None = None
    last_payment: dict | None = None
    last_day: date | None = None
    last_day_consumption: float | None = None
    today_consumption_kwh: float | None = None
    today_cost: float | None = None


@dataclass
class PrepayData:
    """Coordinator payload."""

    meters: dict[str, MeterData] = field(default_factory=dict)


def find_prepay_meters(accounts: list[dict]) -> tuple[list[PrepayMeter], dict[str, int | None]]:
    """Pick prepay smart meters out of the account tree.

    Returns the meters and each meter's weekly debt recovery rate.
    """
    meters: list[PrepayMeter] = []
    debt_rates: dict[str, int | None] = {}
    for account in accounts:
        number = account.get("number")
        if not number:
            continue
        for prop in account.get("properties") or []:
            points = [
                (FUEL_ELECTRICITY, point.get("mpan"), point)
                for point in prop.get("electricityMeterPoints") or []
            ] + [
                (FUEL_GAS, point.get("mprn"), point)
                for point in prop.get("gasMeterPoints") or []
            ]
            for fuel, mpxn, point in points:
                for meter in point.get("meters") or []:
                    for device in meter.get("smartDevices") or []:
                        if (
                            device.get("paymentMode") != "PREPAY"
                            or device.get("type") not in METER_DEVICE_TYPES
                            or not device.get("deviceId")
                        ):
                            continue
                        meters.append(
                            PrepayMeter(
                                device_id=device["deviceId"],
                                fuel=fuel,
                                account_number=number,
                                mpxn=mpxn or "",
                                serial=meter.get("serialNumber") or "",
                                consumption_units=meter.get("consumptionUnits"),
                            )
                        )
                        debt_rates[device["deviceId"]] = device.get(
                            "weeklyDebtRecoveryRateInPence"
                        )
    return meters, debt_rates


class EonNextPrepayCoordinator(DataUpdateCoordinator[PrepayData]):
    """Polls balances, top-ups and usage for every prepay meter on the account."""

    config_entry: EonNextPrepayConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EonNextPrepayConfigEntry,
        client: EonNextClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.meters: dict[str, PrepayMeter] = {}
        # device ids with an in-home display feeding telemetry; sensors are only
        # created for these, so a meter that starts working needs a reload
        self.telemetry_supported: set[str] = set()
        self._telemetry_unsupported: set[str] = set()

    async def _async_update_data(self) -> PrepayData:
        try:
            return await self._fetch()
        except EonNextAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EonNextApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> PrepayData:
        meters, debt_rates = find_prepay_meters(await self.client.async_get_accounts())
        self.meters = {meter.device_id: meter for meter in meters}
        previous = self.data.meters if self.data else {}

        payments_by_serial: dict[str, dict] = {}
        for account_number in {meter.account_number for meter in meters}:
            try:
                payments = await self.client.async_get_payments(account_number)
            except EonNextApiError as err:
                _LOGGER.debug("Top-up history unavailable for account: %s", err)
                continue
            # newest first, so the first payment seen per meter is the latest
            for payment in sorted(
                payments, key=lambda p: p.get("paymentDate") or "", reverse=True
            ):
                serial = (payment.get("meter") or {}).get("serialNumber")
                if serial and serial not in payments_by_serial:
                    payments_by_serial[serial] = payment

        consumption_by_serial: dict[str, list[dict]] | None = {}
        start_at = datetime.combine(
            dt_util.now(UK).date() - timedelta(days=CONSUMPTION_LOOKBACK_DAYS), time.min, UK
        )
        for account_number in {meter.account_number for meter in meters}:
            try:
                consumption_by_serial.update(
                    await self.client.async_get_consumption(
                        account_number, start_at, SUPPLY_TIMEZONE
                    )
                )
            except EonNextApiError as err:
                _LOGGER.debug("Daily consumption unavailable for account: %s", err)
                consumption_by_serial = None
                break

        result = PrepayData()
        for meter in meters:
            data = MeterData(
                balance=await self._fetch_balance(meter),
                weekly_debt_recovery_pence=debt_rates.get(meter.device_id),
                last_payment=payments_by_serial.get(meter.serial),
            )
            await self._fetch_auto_topup(meter, data)
            if consumption_by_serial is None:
                # keep the last known value through a failed fetch
                if old := previous.get(meter.device_id):
                    data.last_day = old.last_day
                    data.last_day_consumption = old.last_day_consumption
            else:
                latest_complete_day(consumption_by_serial.get(meter.serial) or [], data)
            await self._fetch_telemetry(meter, data)
            result.meters[meter.device_id] = data
        return result

    async def _fetch_balance(self, meter: PrepayMeter) -> dict | None:
        try:
            return await self.client.async_get_balance(meter.device_id)
        except EonNextApiError as err:
            if err.codes & BALANCE_MISSING_CODES:
                _LOGGER.debug("No balance snapshot for %s: %s", meter.fuel, err)
                return None
            raise

    async def _fetch_auto_topup(self, meter: PrepayMeter, data: MeterData) -> None:
        try:
            configs = await self.client.async_get_auto_topup_configs(
                meter.account_number, meter.device_id
            )
        except EonNextApiError as err:
            _LOGGER.debug("Auto top-up config unavailable for %s: %s", meter.fuel, err)
            return
        now = dt_util.utcnow()
        for config in configs:
            effective_to = dt_util.parse_datetime(config.get("effectiveTo") or "")
            if effective_to is None or effective_to > now:
                data.auto_topup_pence = config.get("topupAmount")
                return

    async def _fetch_telemetry(self, meter: PrepayMeter, data: MeterData) -> None:
        if meter.device_id in self._telemetry_unsupported:
            return
        now = dt_util.now()
        start = dt_util.start_of_local_day()
        try:
            readings = await self.client.async_get_telemetry(meter.device_id, start, now)
        except EonNextApiError as err:
            if err.codes & TELEMETRY_UNSUPPORTED_CODES:
                self._telemetry_unsupported.add(meter.device_id)
            _LOGGER.debug("Telemetry unavailable for %s: %s", meter.fuel, err)
            return
        self.telemetry_supported.add(meter.device_id)
        # deltas are Wh and pence (same units as Octopus's Kraken telemetry)
        data.today_consumption_kwh = round(
            sum(float(r.get("consumptionDelta") or 0) for r in readings) / 1000, 3
        )
        data.today_cost = round(
            sum(float(r.get("costDeltaWithTax") or 0) for r in readings) / 100, 2
        )


def latest_complete_day(buckets: list[dict], data: MeterData) -> None:
    """Set the most recent whole day's consumption.

    Meter data reaches E.ON in batches a day or two behind, and the newest daily
    bucket is often only part of a day (e.g. 00:00-01:00), so it's never used:
    data arrives in order, so the bucket before it is complete.
    """
    today = dt_util.now(UK).date()
    ordered = sorted(
        (b for b in buckets if b.get("startAt")), key=lambda b: b["startAt"], reverse=True
    )
    for bucket in ordered[1:]:
        start = dt_util.parse_datetime(bucket.get("startAt") or "")
        if start is None or bucket.get("value") is None:
            continue
        # startAt carries the UK offset we asked for, so its date is the UK day
        day = start.date()
        if day >= today:
            continue
        data.last_day = day
        data.last_day_consumption = float(bucket["value"])
        return


def pence_to_pounds(value: int | float | None) -> float | None:
    """Convert pence to pounds, keeping None."""
    return None if value is None else round(value / 100, 2)


def parse_date(value: str | None) -> date | None:
    """Parse an ISO date, tolerating datetimes."""
    if not value:
        return None
    if parsed := dt_util.parse_date(value):
        return parsed
    parsed_dt: datetime | None = dt_util.parse_datetime(value)
    return parsed_dt.date() if parsed_dt else None
