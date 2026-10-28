"""The explicit Phase 1 data contract and fixed synthetic currency assumptions."""

REQUIRED_COLUMNS = (
    "transaction_id",
    "customer_id",
    "timestamp",
    "transaction_amount",
    "merchant_category",
    "currency",
    "country",
)
STRING_COLUMNS = tuple(c for c in REQUIRED_COLUMNS if c not in {"timestamp", "transaction_amount"})
MERCHANT_CATEGORIES = ("groceries", "dining", "retail", "transport", "entertainment", "utilities")
COUNTRY_CURRENCY = {"US": "USD", "GB": "GBP", "DE": "EUR", "IN": "INR"}
# Illustrative fixed conversion rates for this simulation, not live market rates.
UNITS_PER_USD = {"USD": 1.0, "GBP": 0.8, "EUR": 0.9, "INR": 80.0}
