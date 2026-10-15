# E.ON Next Prepay

A Home Assistant custom integration for E.ON Next **smart prepay** meters: credit, debt, emergency credit, top-ups and usage.

It finds every smart meter on your account that is in prepayment mode, gas or electricity, and creates a device for each one.

## Why a refresh token?

E.ON Next moved customer login to Auth0, and their API no longer accepts an email and password. The login page also has a CAPTCHA, so this integration never logs in by itself. Instead:

1. You log in once in a normal browser and copy the session's **refresh token**.
2. The integration swaps it for a short-lived ID token, which it uses to call the API.
3. Auth0 issues a new refresh token every time one is used, and the integration saves it each time. As long as Home Assistant keeps polling, the login keeps renewing itself.

If the chain breaks (a long outage, a password change, or E.ON revoking sessions), Home Assistant shows a **re-authentication** prompt and you paste in a new token.

## Install

1. In HACS, go to ⋮ → **Custom repositories** and add this repo as an **Integration**. Install **E.ON Next Prepay** and restart Home Assistant.
   Without HACS, copy `custom_components/eon_next_prepay` into your HA `config/custom_components/` instead.
2. Get a refresh token (see below).
3. Go to **Settings → Devices & services → Add integration → E.ON Next Prepay** and paste the token.

## Getting a refresh token

### With the helper (recommended)

```bash
uv run tools/get_refresh_token.py
```

This opens Chrome or Chromium with a throwaway profile at the E.ON Next login page. Log in yourself, including the CAPTCHA. Once the dashboard loads, the helper copies the token to your clipboard, closes the browser **without logging out**, and deletes the profile. Use `--browser /path/to/chrome` to pick the browser, or `--print` to print the token instead of copying it.

### By hand

1. Log in at <https://www.eonnext.com> and open DevTools → Console.
2. Run:

   ```js
   Object.keys(localStorage).filter(k=>k.startsWith('@@auth0spajs@@')).map(k=>JSON.parse(localStorage[k]).body?.refresh_token).find(Boolean)
   ```

3. Copy the value without the quotes, then **close the tab without logging out**. Logging out revokes the token.

To replace the token later: **Settings → Devices & services → E.ON Next Prepay → ⋮ → Reconfigure**.

## Entities (per prepay meter)

| Entity | Notes |
| --- | --- |
| Credit | £ remaining on the meter |
| Debt | £ owed on the meter |
| Emergency credit | £ emergency credit available on the meter |
| Weekly debt recovery | £ per week taken towards debt; only created if E.ON reports a rate |
| Last top-up / Last top-up date | latest payment to this meter; status in attributes |
| Auto top-up amount | £; only created if auto top-up is active |
| Usage (last full day) | latest complete UK day the meter has reported; the date is in the `date` attribute. E.ON receives meter data in batches 1–2 days behind, so this is usually yesterday or the day before. kWh, or m³ for gas meters that report volume |
| Usage today / Cost today | only created if E.ON has live telemetry for the meter (needs a linked consumer access device or in-home display); usable in the Energy dashboard |
| Balance updated | when E.ON last took a balance snapshot (diagnostic) |
| Low credit (binary sensor) | on when credit falls below the threshold (default £5) |

**Options:** low-credit threshold, and the update interval (default 30 minutes, minimum 10).

Sensors that depend on an optional feature (auto top-up, debt recovery, live telemetry) are only created if the feature is active when the integration loads. If you turn one on later, reload the integration (⋮ → Reload).

E.ON takes the prepay balance snapshot once a day, at midnight, so the credit figures change daily rather than live.

## Verified so far

Tested against a real account with smart prepay electricity and gas meters:

- Token refresh and rotation work, and the ID token lasts about an hour.
- Both prepay meters were found, and balances matched the E.ON app.
- Balances and top-up amounts are in pence.
- Live telemetry isn't available without a linked in-home display (`KT-GB-4039`); those sensors are skipped.

Still assumed, because the test account didn't have them:

- Auto top-up `topupAmount` is in pence, like the other amounts.
- Telemetry `consumptionDelta` is in Wh and `costDeltaWithTax` is in pence (the same as Octopus, which runs on the same platform).

If something looks wrong, download diagnostics from the integration page. Tokens, account numbers, meter IDs and serials are redacted.

## Credits

- [monsagri/eon-next-v2](https://github.com/monsagri/eon-next-v2) by monsagri and contributors (MIT): the full E.ON Next integration this one is modelled on, and the reference for how E.ON's Kraken API is used. No code was copied; if you're on a credit tariff, use that integration instead.
- DanDon, for suggesting the Auth0 refresh-token flow in [eon-next-v2 issue #98](https://github.com/monsagri/eon-next-v2/issues/98).

## Development

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```

### Testing against a real account

`tools/smoke_test.py` runs every API call the integration makes using its own client, and prints the raw responses with identifiers masked:

```bash
.venv/bin/python tools/smoke_test.py          # prompts for a refresh token
```

The token you give it is used up. The replacement is saved to `eon.token` (gitignored, mode 600). Later runs read it from there automatically, or you can paste it into Home Assistant. You can also pass a token in `EON_REFRESH_TOKEN` instead of being prompted.
