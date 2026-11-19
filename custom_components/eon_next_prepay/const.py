"""Constants for the E.ON Next Prepay integration."""

from __future__ import annotations

DOMAIN = "eon_next_prepay"

# E.ON Next moved customer login to Auth0; the web dashboard's public SPA client.
# Not a secret: it's served to every visitor in eonnext.com's JS and has no client secret.
AUTH0_TOKEN_URL = "https://auth.eonnext.com/oauth/token"
AUTH0_CLIENT_ID = "OrFeFacHUoXK2afczePYMLCRMXpwRxzW"  # gitleaks:allow

API_BASE_URL = "https://api.eonnext-kraken.energy/v1"
GRAPHQL_URL = f"{API_BASE_URL}/graphql/"

# entry data holds exactly one of these: the API key once the user has one,
# otherwise an Auth0 refresh token
CONF_API_KEY = "api_key"
CONF_REFRESH_TOKEN = "refresh_token"
# config flow fields
CONF_TOKEN = "token"
CONF_CREATE_API_KEY = "create_api_key"
CONF_LOW_CREDIT_THRESHOLD = "low_credit_threshold"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_LOW_CREDIT_THRESHOLD = 5.0  # GBP
DEFAULT_SCAN_INTERVAL = 30  # minutes
MIN_SCAN_INTERVAL = 10  # minutes

FUEL_ELECTRICITY = "electricity"
FUEL_GAS = "gas"

# repair issue offering refresh-token entries a switch to an API key
ISSUE_SWITCH_TO_API_KEY = "switch_to_api_key"
