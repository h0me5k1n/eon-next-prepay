# E.ON Next Prepay

A Home Assistant custom integration for E.ON Next **smart prepay** meters: credit, debt, emergency credit, top-ups and usage.

It finds every smart meter on your account that is in prepayment mode, gas or electricity, and creates a device for each one.

## How login works

E.ON Next moved customer login to Auth0, and their API no longer accepts an email and password. The login page also has a CAPTCHA, so this integration never logs in by itself. It uses one of two credentials instead:

- **API key (recommended).** Each E.ON Next account can have one API key (`sk_live_…`). It doesn't expire, so once the integration has it you shouldn't need to log in again. E.ON's website has no page for API keys, so the integration creates one for you (see below).
- **Refresh token.** Copied from a logged-in eonnext.com session. Auth0 replaces it with a new one each time it's used, and the integration saves each new one, but **renewing doesn't extend the login**: it ends about a week after you logged in in the browser, and then Home Assistant asks for a new token. Its main use is to create the API key.

### About the API key

- **One key per account, shared by every app.** Creating a new key cancels the old one everywhere it's used: other integrations, apps, other Home Assistant installs. The integration only creates a key if your account has none. If one exists, it asks what to do, and nothing is replaced unless you choose that.
- **E.ON shows a key only once.** When the integration creates one, it shows it to you once. Save it in a password manager, because you'll need it to set the integration up again (see [Moving to a new Home Assistant install](#moving-to-a-new-home-assistant-install-or-reinstalling)).
- **Treat it like a password.** It gives full access to your E.ON Next account. It's stored in Home Assistant's config, like the refresh token was, and is hidden in diagnostics.
- **It's separate from your website login.** Logging in or out of eonnext.com, or E.ON's app, doesn't affect it. Removing the integration doesn't cancel it either. E.ON has no way to delete a key, only to replace it.

## Install

1. In HACS, go to ⋮ → **Custom repositories** and add this repo as an **Integration**. Install **E.ON Next Prepay** and restart Home Assistant.
   Without HACS, copy `custom_components/eon_next_prepay` into your HA `config/custom_components/` instead.
2. Get a refresh token (see below), or use your API key if you already have one.
3. Go to **Settings → Devices & services → Add integration → E.ON Next Prepay** and paste it. Leave **Create an API key** ticked, and save the key the integration shows you.

## Upgrading from 0.1.x

Nothing changes when you update, and you don't need to paste anything:

- Your existing setup keeps working on its refresh token, as before.
- **Settings → System → Repairs** shows **Switch E.ON Next to an API key**. Submit it and the integration creates a key using its current login. Nothing is created on your E.ON account until you do. If your account already has a key, you're asked whether to enter it, replace it, or carry on with the refresh token.
- If you leave it, the refresh token runs out within a week as before. The re-authentication prompt then accepts a refresh token (and creates a key from it) or an API key.

**Going back to 0.1.x after switching:** 0.1.x doesn't understand API keys, so the integration fails to load. Remove it and add it again with a new refresh token.

## Getting a refresh token

You only need this to create your API key, or if you choose not to use one.

### With the helper (recommended)

```bash
uv run tools/get_refresh_token.py
```

This opens Chrome or Chromium with a throwaway profile at the E.ON Next login page. Log in yourself, including the CAPTCHA. Once the dashboard loads, the helper copies the token to your clipboard, closes the browser **without logging out**, and deletes the profile. Use `--browser /path/to/chrome` to pick the browser, or `--print` to print the token instead of copying it.

### By hand

Use a **private/incognito window**. Otherwise your normal browser keeps a copy of the token, and the next time you visit eonnext.com the site uses that copy. Auth0 treats an already-used token as stolen and ends the login, which logs you out of the website and breaks Home Assistant's login too.

1. In a private window, log in at <https://www.eonnext.com> and open DevTools → Console.
2. Run:

   ```js
   Object.keys(localStorage).filter(k=>k.startsWith('@@auth0spajs@@')).map(k=>JSON.parse(localStorage[k]).body?.refresh_token).find(Boolean)
   ```

3. Copy the value without the quotes, then **close the window without logging out**. Logging out cancels the token.

To change the login later: **Settings → Devices & services → E.ON Next Prepay → ⋮ → Reconfigure**. It accepts an API key or a refresh token.

## Moving to a new Home Assistant install, or reinstalling

- **Restoring a Home Assistant backup:** the API key comes with it, so nothing to do. Running two installs on the same key at once is fine.
- **Fresh setup, and you saved the key:** add the integration and paste the key.
- **Fresh setup, and you don't have the key:** paste a refresh token. The integration sees that the account already has a key and offers to replace it. That's safe if nothing else uses the old key.

With a refresh token rather than a key, don't copy it between installs. Both would use the same login, and the first time one used a token the other had already used, Auth0 would end it for both.

## Troubleshooting login

- **Home Assistant keeps asking for a new refresh token every week or so:** that's the refresh token's limit. Switch to an API key (see [Upgrading from 0.1.x](#upgrading-from-01x)).
- **The eonnext.com website logs you out straight after logging in:** your browser probably still holds a refresh token that Home Assistant has already used (from copying it by hand outside a private window). Clear site data for eonnext.com and auth.eonnext.com, or use a private window.
- **"E.ON Next rejected it" with an API key:** the key was probably replaced by another app or install. Reconfigure with the new key, or with a refresh token to create a new one.

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
- An unknown API key is rejected with `KT-CT-1139`.
- Both prepay meters were found, and balances matched the E.ON app.
- Balances and top-up amounts are in pence.
- Live telemetry isn't available without a linked in-home display (`KT-GB-4039`); those sensors are skipped.

Still assumed:

- A refresh-token login ends about a week after the browser login, however often it's renewed. Other E.ON Next integrations report the same, and so does the test account, but E.ON doesn't publish the limit.
- Creating an API key and logging in with it work as other E.ON Next integrations report. Not yet run against the test account.

Because the test account didn't have them:

- Auto top-up `topupAmount` is in pence, like the other amounts.
- Telemetry `consumptionDelta` is in Wh and `costDeltaWithTax` is in pence (the same as Octopus, which runs on the same platform).

If something looks wrong, download diagnostics from the integration page. Tokens, account numbers, meter IDs and serials are redacted.

## Credits

- [monsagri/eon-next-v2](https://github.com/monsagri/eon-next-v2) by monsagri and contributors (MIT): the full E.ON Next integration this one is modelled on, and the reference for how E.ON's Kraken API is used. No code was copied; if you're on a credit tariff, use that integration instead.
- DanDon, for suggesting the Auth0 refresh-token flow in [eon-next-v2 issue #98](https://github.com/monsagri/eon-next-v2/issues/98).
- baz1536 (EnergyDock), for the research into API-key login and creating keys (`obtainKrakenToken` with `APIKey`, `liveSecretKey`, `regenerateSecretKey`) in [savagelands-net/homeassistant-eon-next issue #1](https://github.com/savagelands-net/homeassistant-eon-next/issues/1), and jonandel for confirming that an Auth0 ID token can create a key, in [eon-next-v2 issue #98](https://github.com/monsagri/eon-next-v2/issues/98).
- [TobySmithRM/eon-next-v2](https://github.com/TobySmithRM/eon-next-v2) (MIT), whose `API_KEY.md` showed the check-before-creating approach to making a key in the browser. No code was copied.

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

It accepts an API key or a refresh token. A refresh token you give it is used up, and the replacement is saved to `eon.token` (gitignored, mode 600). Later runs read the key or token from there automatically. You can also pass `EON_API_KEY` or `EON_REFRESH_TOKEN` instead of being prompted. Don't point it at a refresh token that Home Assistant is also using: whichever uses an old token second ends the login for both.

`--create-api-key` creates an API key from a refresh token (only if the account has none), prints it once and saves it to `eon.token`.
