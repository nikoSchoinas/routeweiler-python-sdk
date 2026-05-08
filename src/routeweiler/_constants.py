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

# FMV snapshot refresh interval — BudgetStore re-fetches provider rates once per day.
FMV_REFRESH_INTERVAL_SECONDS = 86_400
