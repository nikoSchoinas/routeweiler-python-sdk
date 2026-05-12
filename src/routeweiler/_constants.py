"""Package-wide constants shared across modules."""

from decimal import Decimal

HTTP_STATUS_PAYMENT_REQUIRED = 402
HTTP_CLIENT_ERROR_THRESHOLD = 400  # HTTP status codes below this are considered successful

# Reaper fires every 5 seconds to roll back stale reserved draws.
REAPER_INTERVAL_SECONDS = 5

# Added to each draw's TTL at insert time as a clock-skew buffer.
CLOCK_SKEW_BUFFER_SECONDS = 30

# Applied on top of snapshot FMV rate when converting to envelope minor units.
FMV_BUFFER = Decimal("0.05")

# BTC (CoinGecko) snapshot refresh — 5 minutes keeps drift inside the 5 % FMV_BUFFER.
FMV_REFRESH_INTERVAL_BTC_SECONDS = 300

# ECB cross-rate snapshot refresh — ECB publishes once per day, so daily is sufficient.
FMV_REFRESH_INTERVAL_ECB_SECONDS = 86_400
