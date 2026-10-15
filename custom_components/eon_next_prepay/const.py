"""Constants for the E.ON Next Prepay integration."""

from __future__ import annotations

DOMAIN = "eon_next_prepay"

# E.ON Next moved customer login to Auth0; the web dashboard's public SPA client.
# Not a secret: it's served to every visitor in eonnext.com's JS and has no client secret.
AUTH0_TOKEN_URL = "https://auth.eonnext.com/oauth/token"
AUTH0_CLIENT_ID = "OrFeFacHUoXK2afczePYMLCRMXpwRxzW"  # gitleaks:allow

API_BASE_URL = "https://api.eonnext-kraken.energy/v1"
GRAPHQL_URL = f"{API_BASE_URL}/graphql/"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_LOW_CREDIT_THRESHOLD = "low_credit_threshold"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_LOW_CREDIT_THRESHOLD = 5.0  # GBP
DEFAULT_SCAN_INTERVAL = 30  # minutes
MIN_SCAN_INTERVAL = 10  # minutes

FUEL_ELECTRICITY = "electricity"
FUEL_GAS = "gas"
