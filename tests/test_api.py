"""Tests for the E.ON Next API client."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from custom_components.eon_next_prepay.api import (
    EonNextApiError,
    EonNextApiKeyExists,
    EonNextAuthError,
    EonNextClient,
    looks_like_api_key,
)
from custom_components.eon_next_prepay.const import AUTH0_TOKEN_URL, GRAPHQL_URL

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


def kraken_error(code: str) -> tuple[int, dict]:
    return 200, {
        "errors": [{"message": "Invalid data.", "extensions": {"errorCode": code}}],
        "data": {"obtainKrakenToken": None},
    }


async def test_api_key_login_uses_jwt_prefix_and_never_touches_auth0() -> None:
    kraken_jwt = make_jwt()

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        assert url == GRAPHQL_URL
        if "obtainKrakenToken" in kwargs["json"]["query"]:
            assert "headers" not in kwargs
            assert kwargs["json"]["variables"] == {"input": {"APIKey": "sk_live_x"}}
            return 200, {"data": {"obtainKrakenToken": {"token": kraken_jwt}}}
        assert kwargs["headers"]["Authorization"] == f"JWT {kraken_jwt}"
        return 200, {"data": {"viewer": {"accounts": []}}}

    session = FakeSession(handler)
    client = EonNextClient(session, api_key="sk_live_x")  # type: ignore[arg-type]
    assert client.uses_api_key
    assert await client.async_get_accounts() == []
    assert await client.async_get_accounts() == []
    logins = [c for c in session.calls if "obtainKrakenToken" in c[2]["json"]["query"]]
    assert len(logins) == 1


async def test_expired_kraken_token_is_renewed_with_the_key() -> None:
    tokens = iter([make_jwt(exp_in=30), make_jwt()])

    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if "obtainKrakenToken" in kwargs["json"]["query"]:
            return 200, {"data": {"obtainKrakenToken": {"token": next(tokens)}}}
        return 200, {"data": {}}

    session = FakeSession(handler)
    client = EonNextClient(session, api_key="sk_live_x")  # type: ignore[arg-type]
    await client.graphql("{ a }")
    await client.graphql("{ b }")
    logins = [c for c in session.calls if "obtainKrakenToken" in c[2]["json"]["query"]]
    assert len(logins) == 2


async def test_rejected_api_key_raises_auth_error() -> None:
    # KT-CT-1139 is what E.ON returns for an unknown key
    client = EonNextClient(FakeSession(lambda m, u, k: kraken_error("KT-CT-1139")), api_key="sk_live_x")  # type: ignore[arg-type]
    with pytest.raises(EonNextAuthError):
        await client.async_get_accounts()


async def test_other_api_key_login_errors_are_retryable() -> None:
    # e.g. rate limiting must not send the user to re-authenticate
    client = EonNextClient(FakeSession(lambda m, u, k: kraken_error("KT-CT-1199")), api_key="sk_live_x")  # type: ignore[arg-type]
    with pytest.raises(EonNextApiError) as err:
        await client.async_get_accounts()
    assert not isinstance(err.value, EonNextAuthError)
    assert err.value.codes == {"KT-CT-1199"}


def key_handler(existing: str | None, calls: list[str]):
    def handler(method: str, url: str, kwargs: dict) -> tuple[int, Any]:
        if url == AUTH0_TOKEN_URL:
            return auth0_ok(make_jwt(), "rt-2")
        query = kwargs["json"]["query"]
        if "liveSecretKey" in query:
            calls.append("check")
            return 200, {"data": {"viewer": {"liveSecretKey": existing}}}
        assert "regenerateSecretKey" in query
        calls.append("create")
        return 200, {"data": {"regenerateSecretKey": {"key": "sk_live_new"}}}

    return handler


async def test_create_api_key_when_none_exists() -> None:
    calls: list[str] = []
    client = EonNextClient(FakeSession(key_handler(None, calls)), "rt-1")  # type: ignore[arg-type]
    assert await client.async_create_api_key() == "sk_live_new"
    assert calls == ["check", "create"]


async def test_create_api_key_refuses_to_replace_existing_key() -> None:
    calls: list[str] = []
    client = EonNextClient(FakeSession(key_handler("sk_live_****abcd", calls)), "rt-1")  # type: ignore[arg-type]
    with pytest.raises(EonNextApiKeyExists) as err:
        await client.async_create_api_key()
    assert err.value.masked == "sk_live_****abcd"
    assert calls == ["check"]

    assert await client.async_create_api_key(replace=True) == "sk_live_new"
    assert calls == ["check", "create"]


def test_looks_like_api_key() -> None:
    assert looks_like_api_key(" sk_live_abc ")
    assert not looks_like_api_key("v1.Mabc-refresh-token")


def test_client_needs_a_credential() -> None:
    with pytest.raises(ValueError):
        EonNextClient(FakeSession(lambda m, u, k: (200, {})))  # type: ignore[arg-type]
