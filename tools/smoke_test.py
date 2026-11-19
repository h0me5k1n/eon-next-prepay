"""Run every API call the integration makes against a real account and show the results.

Uses the integration's own client, so it checks the real code paths: login
(API key or refresh token), prepay meter discovery, balance, top-ups, auto
top-up, telemetry and daily consumption.

Give it an API key (sk_live_...) or a refresh token. A refresh token is spent on
first use. The rotated one is written to the token file straight away (mode 600,
gitignored), so re-run with the same file, or paste its contents into Home
Assistant - don't reuse the original token, and don't use a token file whose
chain Home Assistant is also using (reusing an old token ends the login).

--create-api-key creates an API key from a refresh token, only if the account
has none, prints it once and saves it to the token file in place of the token.

Usage (from the repo root, with the test venv):
    .venv/bin/python tools/smoke_test.py                   # prompts for the key/token
    EON_REFRESH_TOKEN=... .venv/bin/python tools/smoke_test.py
    EON_API_KEY=sk_live_... .venv/bin/python tools/smoke_test.py
    .venv/bin/python tools/smoke_test.py --token-file eon.token   # reuse saved key/token
    .venv/bin/python tools/smoke_test.py --create-api-key

Account numbers, meter points, serials and device IDs are masked so the output
can be shared.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import getpass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.eon_next_prepay.api import (  # noqa: E402
    EonNextApiError,
    EonNextApiKeyExists,
    EonNextAuthError,
    EonNextClient,
    _jwt_expiry,
    looks_like_api_key,
)
from custom_components.eon_next_prepay.coordinator import find_prepay_meters  # noqa: E402

SENSITIVE_KEYS = {"number", "mpan", "mprn", "serialNumber", "deviceId", "utrn"}


def mask(value: Any) -> Any:
    if not isinstance(value, str) or len(value) <= 4:
        return value
    return "…" + value[-4:]


def masked(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: mask(v) if k in SENSITIVE_KEYS else masked(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [masked(v) for v in obj]
    return obj


def show(title: str, obj: Any) -> None:
    print(f"\n== {title}")
    print(json.dumps(masked(obj), indent=2, default=str))


async def step(title: str, coro: Any) -> Any:
    try:
        result = await coro
    except EonNextAuthError:
        raise
    except EonNextApiError as err:
        print(f"\n== {title}\n   FAILED: {err} codes={sorted(err.codes) or '-'}")
        return None
    return result


def save_token(path: Path, token: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token)
    tmp.chmod(0o600)
    tmp.replace(path)


async def run(token: str, token_file: Path, create_api_key: bool = False) -> int:
    rotations = 0

    def on_rotate(new_token: str) -> None:
        nonlocal rotations
        rotations += 1
        save_token(token_file, new_token)

    async with aiohttp.ClientSession() as session:
        if looks_like_api_key(token):
            client = EonNextClient(session, api_key=token)
        else:
            client = EonNextClient(session, token, on_rotate)
        try:
            accounts = await client.async_get_accounts()
        except EonNextAuthError as err:
            print(f"Login rejected: {err}")
            return 1

        if client.uses_api_key:
            print("API key login OK.")
        else:
            print(f"Token refresh OK. Refresh token rotated: {'yes' if rotations else 'no'} "
                  f"(saved to {token_file})")
        print(f"Access token lifetime: ~{(_jwt_expiry(client._id_token or '') - time.time()) / 60:.0f} min")

        if create_api_key:
            if client.uses_api_key:
                print("--create-api-key needs a refresh token, not an API key")
                return 1
            try:
                key = await client.async_create_api_key()
            except EonNextApiKeyExists as err:
                print(f"Not creating an API key: the account already has one ({err.masked}). "
                      "Creating another would cancel it for everything using it.")
                return 1
            save_token(token_file, key)
            print(f"\nAPI key created (saved to {token_file}). E.ON won't show it again:\n\n    {key}\n")
            return 0

        show("Accounts / meters", accounts)

        meters, debt_rates = find_prepay_meters(accounts)
        print(f"\nPrepay meters found: {len(meters)}")
        for meter in meters:
            print(f"   {meter.fuel}: device {mask(meter.device_id)}, serial {mask(meter.serial)}, "
                  f"units {meter.consumption_units!r}, weekly debt recovery {debt_rates.get(meter.device_id)}p")

        for account in sorted({m.account_number for m in meters}):
            payments = await step("Payments", client.async_get_payments(account))
            if payments is not None:
                show(f"Payments for account {mask(account)} (latest 5 by date)",
                     sorted(payments, key=lambda p: p.get("paymentDate") or "", reverse=True)[:5])

        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily: dict[str, list[dict]] = {}
        half_hourly: dict[str, list[dict]] = {}
        for account in sorted({m.account_number for m in meters}):
            result = await step(
                "Daily consumption",
                client.async_get_consumption(account, midnight - timedelta(days=7), "Europe/London"),
            )
            daily.update(result or {})
            # two days of half-hours, to see how far behind the meter data is
            result = await step(
                "Half-hourly consumption",
                client.async_get_consumption(
                    account, midnight - timedelta(days=1), "Europe/London", "HALF_HOUR", 100
                ),
            )
            half_hourly.update(result or {})
        for meter in meters:
            label = f"{meter.fuel} {mask(meter.device_id)}"
            balance = await step(f"Balance {label}", client.async_get_balance(meter.device_id))
            if balance is not None:
                show(f"Balance {label}", balance)
                print(f"   -> credit £{(balance.get('creditInPence') or 0) / 100:.2f}, "
                      f"debt £{(balance.get('debtInPence') or 0) / 100:.2f}, "
                      f"emergency £{(balance.get('emergencyCreditInPence') or 0) / 100:.2f}")

            configs = await step(f"Auto top-up {label}",
                                 client.async_get_auto_topup_configs(meter.account_number, meter.device_id))
            if configs is not None:
                show(f"Auto top-up {label}", configs)

            telemetry = await step(f"Telemetry today {label}",
                                   client.async_get_telemetry(meter.device_id, midnight, now))
            if telemetry is not None:
                show(f"Telemetry today {label} ({len(telemetry)} readings, first/last 2)",
                     telemetry[:2] + telemetry[-2:])
                kwh = sum(float(r.get("consumptionDelta") or 0) for r in telemetry) / 1000
                cost = sum(float(r.get("costDeltaWithTax") or 0) for r in telemetry) / 100
                print(f"   -> today {kwh:.3f} kWh, £{cost:.2f} (assuming Wh and pence)")

            if (buckets := daily.get(meter.serial)) is not None:
                print(f"\n== Daily consumption {label} ({meter.consumption_units})")
                for b in sorted(buckets, key=lambda b: b["startAt"]):
                    print(f"   {b['startAt'][:10]}  {float(b['value']):8.3f}")

            if (slots := half_hourly.get(meter.serial)) is not None:
                slots = sorted(slots, key=lambda b: b["startAt"])
                print(f"\n== Half-hourly consumption {label}: {len(slots)} slots since yesterday midnight")
                if slots:
                    latest = datetime.fromisoformat(slots[-1]["endAt"])
                    today = [b for b in slots if datetime.fromisoformat(b["startAt"]) >= midnight]
                    print(f"   latest slot ends {latest.isoformat()} "
                          f"({(now - latest).total_seconds() / 3600:.1f} h behind now)")
                    print(f"   today so far: {len(today)} slots, "
                          f"{sum(float(b['value']) for b in today):.3f} {meter.consumption_units}")

    if not client.uses_api_key:
        print(f"\nDone. Latest refresh token is in {token_file} - use it next run or paste it into HA.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-file", type=Path, default=Path("eon.token"),
                        help="where the API key or rotated token is kept (default: eon.token); read from "
                             "if it exists and neither EON_API_KEY nor EON_REFRESH_TOKEN is set")
    parser.add_argument("--create-api-key", action="store_true",
                        help="create an API key from the refresh token (only if the account has none)")
    args = parser.parse_args()

    token = (os.environ.get("EON_API_KEY") or os.environ.get("EON_REFRESH_TOKEN") or "").strip()
    if not token and args.token_file.exists():
        token = args.token_file.read_text().strip()
        print(f"Using the {'API key' if looks_like_api_key(token) else 'refresh token'} from {args.token_file}")
    if not token:
        # getpass keeps it out of shell history and the terminal scrollback
        token = getpass.getpass("E.ON Next API key or refresh token: ").strip()
    if not token:
        sys.exit("No API key or refresh token given")
    sys.exit(asyncio.run(run(token, args.token_file, args.create_api_key)))


if __name__ == "__main__":
    main()
