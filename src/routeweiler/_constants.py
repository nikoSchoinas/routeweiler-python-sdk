"""Package-wide constants shared across modules."""

from decimal import Decimal

HTTP_STATUS_PAYMENT_REQUIRED = 402
HTTP_CLIENT_ERROR_THRESHOLD = 400  # HTTP status codes below this are considered successful

# Reaper fires every 5 seconds to roll back stale reserved draws (§8.3).
REAPER_INTERVAL_SECONDS = 5

# Added to each draw's TTL at insert time as a clock-skew buffer (§8.4).
CLOCK_SKEW_BUFFER_SECONDS = 30

# Applied on top of snapshot FMV rate when converting to envelope minor units (§8.4).
FMV_BUFFER = Decimal("0.05")

# FMV snapshot refresh interval — BudgetStore re-fetches provider rates once per day (§17).
FMV_REFRESH_INTERVAL_SECONDS = 86_400
