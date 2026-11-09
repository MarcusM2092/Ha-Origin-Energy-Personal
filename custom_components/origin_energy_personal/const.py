"""Constants for the personal Origin Energy integration.

Approach and API findings are based on JKook-Plus/ha-origin-energy (MIT).
"""
from __future__ import annotations


# Deliberately NOT "origin_energy", so this can coexist with the public
# integration (same domain would make the two fight over the folder).
DOMAIN = "origin_energy_personal"

CONF_COOKIE = "auth0_cookie"
CONF_AGREEMENT_ID = "agreement_id"
CONF_NMI = "nmi"
CONF_SCAN_HOURS = "scan_hours"
DEFAULT_SCAN_HOURS = 1

GRAPHQL_URL = "https://api.rx.originenergy.com.au/v1/gateway/graphql"

AUTH_DOMAIN = "id.originenergy.com.au"
APP_DOMAIN = "www.originenergy.com.au"
AUTHORIZE_URL = f"https://{AUTH_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH_DOMAIN}/oauth/token"
SESSION_COOKIE_NAME = "auth0"

# Public SPA values used by originenergy.com.au's own My Account web app
# (a public OAuth client id, not a credential), as documented by the original
# project. If Origin ever rotates them, re-capture from the Network tab: they
# are query parameters of the /authorize request.
CLIENT_ID = "yOHRT97N3yH85jzTDlqN2A7Cf2D0cmQe"
AUDIENCE = "https://digitalapi"
REDIRECT_URI = f"https://{APP_DOMAIN}/auth/callback"
SCOPE = "openid profile email read:api all"

# auth0-spa-js telemetry param the SPA sends; decodes to
# {"name":"auth0-spa-js","version":"1.10.0"}. Static, identical for everyone.
AUTH0_SPA_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMTAuMCJ9"

# Origin's WAF returns 403 to non-browser clients on /authorize. These are the
# headers the original project verified against the live site (a Chrome
# identity, even if you use Firefox: the WAF cares that it looks like the SPA's
# own traffic). Sent per request, because Home Assistant discards
# session-level default headers.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": f"https://{APP_DOMAIN}",
    "Referer": f"https://{APP_DOMAIN}/",
}

# Usage query limits measured by the original author:
#   sub-daily windows fail beyond 9 days, so stay at 7.
HOURLY_CHUNK_DAYS = 7
FIRST_RUN_HISTORY_DAYS = 730   # upper cap; real start is the agreement's validFrom
TRAILING_DAYS = 5              # re-read this far back from the newest stored hour

REQUEST_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 5

# --- Gas and tariff rates (best-effort extras; see coordinator.py) ------------
# Gas and rate lookups run in their own error-isolated layer, so a failure
# there is logged and never affects the electricity import.
EXTRAS_REFRESH_HOURS = 12      # how often to re-read the gas service and tariffs
GAS_CHUNK_DAYS = {"HOURLY": 7, "DAILY": 365}   # per-query windows (limits: 9d / 1000 points)

# UNVERIFIED: whether rateIncludingTax is in cents (True) or dollars (False).
# Compare a rate sensor with your bill; flip this if it is off by 100x.
RATES_IN_CENTS = True
