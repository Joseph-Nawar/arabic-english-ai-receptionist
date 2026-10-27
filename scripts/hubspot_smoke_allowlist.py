"""Reviewed non-production HubSpot portal IDs allowed for manual smoke runs."""

# Add a developer test portal only after its account and Service Key scope review.
# The empty default intentionally disables live smoke writes until that review occurs.
REVIEWED_DEVELOPER_PORTAL_IDS: frozenset[str] = frozenset()
