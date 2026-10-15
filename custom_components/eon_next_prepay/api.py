"""Client for the E.ON Next Kraken API, authenticated with an Auth0 refresh token.

Auth flow (since E.ON Next moved login to Auth0):
    refresh_token -> auth.eonnext.com/oauth/token -> id_token -> Authorization header

Auth0 rotates refresh tokens: every exchange returns a new one and the old one
stops working, so callers must persist ``refresh_token`` after each refresh
(the ``on_refresh_token`` callback fires whenever it changes).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import aiohttp

from .const import AUTH0_CLIENT_ID, AUTH0_TOKEN_URL, GRAPHQL_URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# refresh the id_token this many seconds before it expires
TOKEN_EXPIRY_MARGIN = 120
# Kraken error codes meaning the id_token was rejected - refresh once and retry
TOKEN_REJECTED_CODES = {"KT-CT-1124", "KT-CT-1143"}
# Auth0 errors meaning the refresh token itself is dead - user must log in again
AUTH0_FATAL_ERRORS = {"invalid_grant", "unauthorized_client", "access_denied"}

QUERY_PREPAY_DEVICES = """
query prepayDevices {
  viewer {
    accounts {
      ... on AccountType {
        number
        properties {
          electricityMeterPoints {
            mpan
            meters(includeInactive: false) {
              serialNumber
              consumptionUnits
              smartDevices { deviceId type paymentMode weeklyDebtRecoveryRateInPence }
            }
          }
          gasMeterPoints {
            mprn
            meters(includeInactive: false) {
              serialNumber
              consumptionUnits
              smartDevices { deviceId type paymentMode weeklyDebtRecoveryRateInPence }
            }
          }
        }
      }
    }
  }
}
"""

QUERY_BALANCE = """
query prepayBalance($deviceId: String!) {
  prepayBalanceSnapshot(deviceId: $deviceId) {
    asAt
    creditInPence
    debtInPence
    emergencyCreditInPence
  }
}
"""

QUERY_PAYMENTS = """
query prepayPayments($accountNumber: String!) {
  prepayPayments(accountNumber: $accountNumber, first: 20) {
    edges {
      node {
        amount
        paymentDate
        smartPrepayProcessStatus
        meter { serialNumber }
      }
    }
  }
}
"""

QUERY_AUTO_TOPUP = """
query autoTopup($accountNumber: String!, $deviceId: String!) {
  autoTopupConfigs(accountNumber: $accountNumber, deviceId: $deviceId, first: 5) {
    edges { node { topupAmount effectiveFrom effectiveTo } }
  }
}
"""

QUERY_TELEMETRY = """
query telemetry($deviceId: String!, $start: DateTime!, $end: DateTime!) {
  smartMeterTelemetry(deviceId: $deviceId, start: $start, end: $end, grouping: HALF_HOURLY) {
    readAt
    consumptionDelta
    costDeltaWithTax
  }
}
"""

METER_CONSUMPTION = """
        meters(includeInactive: false) {
          serialNumber
          consumption(startAt: $startAt, grouping: $grouping, timezone: $timezone, first: $first) {
            edges { node { value startAt endAt } }
          }
        }"""

QUERY_CONSUMPTION = f"""
query consumption(
  $accountNumber: String!, $startAt: DateTime!, $timezone: String!,
  $grouping: ConsumptionGroupings!, $first: Int!
) {{
  account(accountNumber: $accountNumber) {{
    properties {{
      electricityMeterPoints {{{METER_CONSUMPTION}
      }}
      gasMeterPoints {{{METER_CONSUMPTION}
      }}
    }}
  }}
}}
"""


class EonNextError(Exception):
    """Base error for the E.ON Next API."""


class EonNextAuthError(EonNextError):
    """The refresh token was rejected; a new one is needed."""


class EonNextApiError(EonNextError):
    """A request failed or the API returned errors."""

    def __init__(self, message: str, codes: set[str] | None = None) -> None:
        super().__init__(message)
        self.codes = codes or set()


def _jwt_expiry(token: str) -> float:
    """Return the exp claim of a JWT (unverified), or a short default."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return time.time() + 300


class EonNextClient:
    """Minimal async client for the prepay parts of the E.ON Next API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._id_token: str | None = None
        self._id_token_expiry = 0.0
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str:
        """Latest refresh token (rotates on every token exchange)."""
        return self._refresh_token

    async def _exchange_refresh_token(self) -> None:
        payload = {
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": self._refresh_token,
        }
        try:
            async with self._session.post(
                AUTH0_TOKEN_URL, json=payload, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EonNextApiError(f"Token refresh request failed: {err}") from err

        if not isinstance(body, dict):
            body = {}
        if body.get("error") in AUTH0_FATAL_ERRORS:
            raise EonNextAuthError(body.get("error_description") or body["error"])
        id_token = body.get("id_token")
        if status != 200 or not id_token:
            raise EonNextApiError(f"Token refresh failed (HTTP {status})")

        self._id_token = id_token
        self._id_token_expiry = _jwt_expiry(id_token)
        new_refresh_token = body.get("refresh_token")
        if new_refresh_token and new_refresh_token != self._refresh_token:
            self._refresh_token = new_refresh_token
            if self._on_refresh_token:
                self._on_refresh_token(new_refresh_token)

    async def _get_id_token(self, rejected: str | None = None) -> str:
        """Return a valid id_token, refreshing if expired or if ``rejected`` is current."""
        async with self._lock:
            if (
                self._id_token is None
                or self._id_token == rejected
                or time.time() > self._id_token_expiry - TOKEN_EXPIRY_MARGIN
            ):
                await self._exchange_refresh_token()
            assert self._id_token is not None
            return self._id_token

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """Run a GraphQL query and return its data, raising on any errors."""
        rejected: str | None = None
        for attempt in range(2):
            token = await self._get_id_token(rejected)
            try:
                async with self._session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables or {}},
                    headers={"Authorization": token},
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    status = resp.status
                    body = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise EonNextApiError(f"GraphQL request failed: {err}") from err

            if not isinstance(body, dict):
                raise EonNextApiError(f"Unexpected GraphQL response (HTTP {status})")
            errors = body.get("errors") or []
            codes = {
                code
                for err in errors
                if (code := (err.get("extensions") or {}).get("errorCode"))
            }
            if attempt == 0 and (status == 401 or codes & TOKEN_REJECTED_CODES):
                rejected = token
                continue
            if errors:
                messages = "; ".join(err.get("message", "unknown error") for err in errors)
                raise EonNextApiError(messages, codes)
            if status != 200:
                raise EonNextApiError(f"GraphQL request failed (HTTP {status})")
            return body.get("data") or {}
        raise EonNextApiError("GraphQL token rejected after refresh", codes)


    # --- prepay queries -------------------------------------------------

    async def async_get_accounts(self) -> list[dict]:
        """Accounts with their meter points, meters and smart devices."""
        data = await self.graphql(QUERY_PREPAY_DEVICES)
        return ((data.get("viewer") or {}).get("accounts")) or []

    async def async_get_balance(self, device_id: str) -> dict | None:
        data = await self.graphql(QUERY_BALANCE, {"deviceId": device_id})
        return data.get("prepayBalanceSnapshot")

    async def async_get_payments(self, account_number: str) -> list[dict]:
        data = await self.graphql(QUERY_PAYMENTS, {"accountNumber": account_number})
        edges = ((data.get("prepayPayments") or {}).get("edges")) or []
        return [edge["node"] for edge in edges if edge and edge.get("node")]

    async def async_get_auto_topup_configs(
        self, account_number: str, device_id: str
    ) -> list[dict]:
        data = await self.graphql(
            QUERY_AUTO_TOPUP, {"accountNumber": account_number, "deviceId": device_id}
        )
        edges = ((data.get("autoTopupConfigs") or {}).get("edges")) or []
        return [edge["node"] for edge in edges if edge and edge.get("node")]

    async def async_get_telemetry(
        self, device_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        data = await self.graphql(
            QUERY_TELEMETRY,
            {"deviceId": device_id, "start": start.isoformat(), "end": end.isoformat()},
        )
        return data.get("smartMeterTelemetry") or []

    async def async_get_consumption(
        self,
        account_number: str,
        start_at: datetime,
        timezone: str,
        grouping: str = "DAY",
        first: int = 14,
    ) -> dict[str, list[dict]]:
        """Consumption buckets per meter serial number, from ``start_at``.

        Buckets come back in no particular order - sort by ``startAt``.
        """
        data = await self.graphql(
            QUERY_CONSUMPTION,
            {
                "accountNumber": account_number,
                "startAt": start_at.isoformat(),
                "timezone": timezone,
                "grouping": grouping,
                "first": first,
            },
        )
        by_serial: dict[str, list[dict]] = {}
        for prop in (data.get("account") or {}).get("properties") or []:
            for kind in ("electricityMeterPoints", "gasMeterPoints"):
                for point in prop.get(kind) or []:
                    for meter in point.get("meters") or []:
                        edges = (meter.get("consumption") or {}).get("edges") or []
                        if serial := meter.get("serialNumber"):
                            by_serial[serial] = [e["node"] for e in edges if e and e.get("node")]
        return by_serial
