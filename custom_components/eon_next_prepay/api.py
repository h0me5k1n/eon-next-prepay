"""Client for the E.ON Next Kraken API.

Two ways to log in:

API key (preferred):
    api_key -> obtainKrakenToken(APIKey) -> Kraken JWT -> "Authorization: JWT <token>"
    The key doesn't expire, so a new JWT is simply requested whenever the old
    one runs out. Each account has a single key, shared by every app using it.

Auth0 refresh token (from a logged-in eonnext.com session):
    refresh_token -> auth.eonnext.com/oauth/token -> id_token -> Authorization header
    Auth0 rotates refresh tokens: every exchange returns a new one and the old
    one stops working, so callers must persist ``refresh_token`` after each
    refresh (the ``on_refresh_token`` callback fires whenever it changes).
    Rotation doesn't extend the login: the whole chain ends when the original
    browser login's maximum lifetime runs out (about a week for E.ON Next).
    A refresh-token client can create an API key with ``async_create_api_key``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
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
# obtainKrakenToken errors meaning the API key was rejected (bad, or replaced)
API_KEY_REJECTED_CODES = {"KT-CT-1138", "KT-CT-1139"}
# E.ON Next API keys look like sk_live_...
API_KEY_PREFIX = "sk_"

MUTATION_API_KEY_LOGIN = """
mutation apiKeyLogin($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) { token }
}
"""

QUERY_EXISTING_API_KEY = """
query existingApiKey { viewer { liveSecretKey } }
"""

# replaces any key the account already has - check QUERY_EXISTING_API_KEY first
MUTATION_REGENERATE_API_KEY = """
mutation regenerateApiKey { regenerateSecretKey { key } }
"""

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


class EonNextApiKeyExists(EonNextError):
    """The account already has an API key, so one wasn't created.

    ``masked`` is the API's masked form of the existing key (never usable).
    """

    def __init__(self, masked: str) -> None:
        super().__init__("The account already has an API key")
        self.masked = masked


def looks_like_api_key(value: str) -> bool:
    """True for an E.ON Next API key, as opposed to an Auth0 refresh token."""
    return value.strip().startswith(API_KEY_PREFIX)


# how much of a non-JSON response to put in the error message
SNIPPET_LENGTH = 200


def _snippet(text: str) -> str:
    """Readable start of an HTML or text error page, for log messages."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:SNIPPET_LENGTH] + ("…" if len(text) > SNIPPET_LENGTH else "") or "(empty)"


async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    """Parse a JSON response, explaining what came back if it isn't JSON.

    E.ON's CDN answers outages and blocked requests with an HTML page, so the
    status, content type, CDN verdict and the page's text say which it was.
    """
    text = await resp.text()
    try:
        return json.loads(text)
    except ValueError:
        details = [f"HTTP {resp.status}", resp.content_type or "no content type"]
        if cache := resp.headers.get("X-Cache"):
            details.append(f"X-Cache: {cache}")
        raise EonNextApiError(
            f"E.ON Next sent a response that isn't JSON ({', '.join(details)}): {_snippet(text)}"
        ) from None


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
        refresh_token: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
        *,
        api_key: str | None = None,
    ) -> None:
        if not (api_key or refresh_token):
            raise ValueError("An API key or a refresh token is required")
        self._session = session
        self._api_key = api_key
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        # Kraken JWT (API key) or Auth0 id_token (refresh token)
        self._id_token: str | None = None
        self._id_token_expiry = 0.0
        self._lock = asyncio.Lock()

    @property
    def uses_api_key(self) -> bool:
        """True if this client logs in with an API key."""
        return self._api_key is not None

    @property
    def refresh_token(self) -> str | None:
        """Latest refresh token (rotates on every token exchange)."""
        return self._refresh_token

    def _authorization(self, token: str) -> str:
        # Kraken JWTs need the JWT prefix; Auth0 id_tokens are sent bare
        return f"JWT {token}" if self.uses_api_key else token

    async def _obtain_kraken_token(self) -> None:
        try:
            async with self._session.post(
                GRAPHQL_URL,
                json={
                    "query": MUTATION_API_KEY_LOGIN,
                    "variables": {"input": {"APIKey": self._api_key}},
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                status = resp.status
                body = await _read_json(resp)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise EonNextApiError(f"API key login request failed: {err}") from err

        if not isinstance(body, dict):
            raise EonNextApiError(f"Unexpected API key login response (HTTP {status})")
        errors = body.get("errors") or []
        codes = {
            code for err in errors if (code := (err.get("extensions") or {}).get("errorCode"))
        }
        if codes & API_KEY_REJECTED_CODES:
            raise EonNextAuthError("E.ON Next rejected the API key")
        token = ((body.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
        if errors or not token:
            # anything else (rate limiting, an outage) is worth retrying later
            raise EonNextApiError(f"API key login failed (HTTP {status})", codes)
        self._id_token = token
        self._id_token_expiry = _jwt_expiry(token)

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
                body = await _read_json(resp)
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
                if self.uses_api_key:
                    await self._obtain_kraken_token()
                else:
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
                    headers={"Authorization": self._authorization(token)},
                    timeout=REQUEST_TIMEOUT,
                ) as resp:
                    status = resp.status
                    body = await _read_json(resp)
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

    # --- API key --------------------------------------------------------

    async def async_get_existing_api_key(self) -> str | None:
        """The account's existing API key, masked, or None if it has none."""
        data = await self.graphql(QUERY_EXISTING_API_KEY)
        return (data.get("viewer") or {}).get("liveSecretKey") or None

    async def async_create_api_key(self, replace: bool = False) -> str:
        """Create an API key for the account and return it.

        E.ON only shows the key once, and each account has a single key:
        creating one cancels the old key for every app using it. So unless
        ``replace`` is set, this raises EonNextApiKeyExists if a key exists.
        """
        if not replace and (existing := await self.async_get_existing_api_key()):
            raise EonNextApiKeyExists(existing)
        data = await self.graphql(MUTATION_REGENERATE_API_KEY)
        key = (data.get("regenerateSecretKey") or {}).get("key")
        if not isinstance(key, str) or not looks_like_api_key(key):
            raise EonNextApiError("E.ON Next didn't return an API key")
        return key

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
