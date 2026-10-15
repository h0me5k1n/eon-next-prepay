"""Tests for the E.ON Next API client."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from custom_components.eon_next_prepay.api import (
    EonNextApiError,
    EonNextAuthError,
    EonNextClient,
)
from custom_components.eon_next_prepay.const import AUTH0_TOKEN_URL

from .conftest import make_jwt


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    """Routes requests to handlers and records them."""

    def __init__(self, handler: Callable[[str, str, dict], tuple[int, Any]]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict]] = []

    def _request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return FakeResponse(*self.handler(method, url, kwargs))

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._request("GET", url, **kwargs)


def auth0_ok(id_token: str, refresh_token: str) -> tuple[int, dict]:
    return 200, {"id_token": id_token, "refresh_token": refresh_token, "access_token": "x"}


async def test_refresh_rotates_token_and_uses_raw_id_token() -> None:
    id_token = make_jwt()
    saved: list[str] = []

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            assert kwargs["json"]["refresh_token"] == "rt-1"
            return auth0_ok(id_token, "rt-2")
        assert kwargs["headers"]["Authorization"] == id_token
        return 200, {"data": {"viewer": {"accounts": []}}}

    session = FakeSession(handler)
    client = EonNextClient(session, "rt-1", saved.append)  # type: ignore[arg-type]

    assert await client.async_get_accounts() == []
    assert await client.async_get_accounts() == []
    assert client.refresh_token == "rt-2"
    assert saved == ["rt-2"]
    # second query reuses the still-valid id_token
    assert [url for _, url, _ in session.calls].count(AUTH0_TOKEN_URL) == 1


async def test_expiring_id_token_is_refreshed() -> None:
    tokens = iter([("rt-2", make_jwt(exp_in=30)), ("rt-3", make_jwt())])

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            refresh, id_token = next(tokens)
            return auth0_ok(id_token, refresh)
        return 200, {"data": {}}

    session = FakeSession(handler)
    client = EonNextClient(session, "rt-1")  # type: ignore[arg-type]
    await client.graphql("{ a }")
    await client.graphql("{ b }")
    assert client.refresh_token == "rt-3"


async def test_dead_refresh_token_raises_auth_error() -> None:
    session = FakeSession(
        lambda m, u, k: (403, {"error": "invalid_grant", "error_description": "Unknown or invalid refresh token."})
    )
    client = EonNextClient(session, "rt-1")  # type: ignore[arg-type]
    with pytest.raises(EonNextAuthError):
        await client.async_get_accounts()


async def test_rejected_id_token_refreshes_and_retries_once() -> None:
    first, second = make_jwt(), make_jwt()
    tokens = iter([("rt-2", first), ("rt-3", second)])

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            refresh, id_token = next(tokens)
            return auth0_ok(id_token, refresh)
        if kwargs["headers"]["Authorization"] == first:
            return 200, {"errors": [{"message": "bad", "extensions": {"errorCode": "KT-CT-1143"}}]}
        return 200, {"data": {"ok": True}}

    client = EonNextClient(FakeSession(handler), "rt-1")  # type: ignore[arg-type]
    assert await client.graphql("{ ok }") == {"ok": True}
    assert client.refresh_token == "rt-3"


async def test_graphql_errors_carry_codes() -> None:
    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            return auth0_ok(make_jwt(), "rt-2")
        return 200, {"errors": [{"message": "Not prepay", "extensions": {"errorCode": "KT-GB-4048"}}]}

    client = EonNextClient(FakeSession(handler), "rt-1")  # type: ignore[arg-type]
    with pytest.raises(EonNextApiError) as err:
        await client.async_get_balance("dev")
    assert err.value.codes == {"KT-GB-4048"}


async def test_daily_consumption_grouped_by_serial() -> None:
    from datetime import UTC, datetime

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            return auth0_ok(make_jwt(), "rt-2")
        variables = kwargs["json"]["variables"]
        assert variables["accountNumber"] == "A-1"
        assert variables["timezone"] == "Europe/London"
        node = {"value": "1.5", "startAt": "2026-10-14T00:00:00+01:00", "endAt": "2026-10-15T00:00:00+01:00"}

        def meter(serial: str) -> dict:
            return {"serialNumber": serial, "consumption": {"edges": [{"node": node}]}}

        return 200, {"data": {"account": {"properties": [{
            "electricityMeterPoints": [{"meters": [meter("E1")]}],
            "gasMeterPoints": [{"meters": [meter("G1"), {"serialNumber": "G2", "consumption": None}]}],
        }]}}}

    session = FakeSession(handler)
    client = EonNextClient(session, "rt-1")  # type: ignore[arg-type]
    result = await client.async_get_consumption(
        "A-1", datetime(2026, 10, 8, tzinfo=UTC), "Europe/London"
    )
    assert set(result) == {"E1", "G1", "G2"}
    assert result["G1"][0]["value"] == "1.5"
    assert result["G2"] == []
    assert all(method == "POST" for method, _, _ in session.calls)
