from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from receptionist.domain.booking_policy import (
    AvailabilityDecision,
    BusinessConfigView,
    CatalogSelectorAmbiguous,
    RequestedInterval,
    ServiceCatalogView,
    booking_state_fingerprint,
    canonical_booking_snapshot,
    decide_availability,
    evaluate_booking_policy,
    intervals_overlap,
    match_catalog_selector,
    normalize_requested_interval,
    validate_required_booking_details,
    validate_service_area_eligibility,
    validate_service_eligibility,
)
from receptionist.domain.config import (
    BookingPolicySpec,
    BookingRequirementSpec,
    ServiceAreaSpec,
    ServiceSpec,
    TimeWindow,
    WeeklyHours,
)
from receptionist.domain.enums import BookingStatus, Weekday
from receptionist.integrations.google_calendar import CalendarInterval
from receptionist.seed import build_reference_business, build_reference_services

pytestmark = pytest.mark.unit


def _service(*, active: bool = True, bookable: bool = True) -> ServiceSpec:
    return build_reference_services()[1].model_copy(update={"active": active, "bookable": bookable})


def _interval_at(local_date: date, start: time, duration_minutes: int = 60) -> RequestedInterval:
    timezone = ZoneInfo("Asia/Riyadh")
    start_at = datetime.combine(local_date, start, tzinfo=timezone).astimezone(UTC)
    return normalize_requested_interval(start_at, None, duration_minutes)


def test_catalog_matching_is_exact_normalized_and_ambiguity_is_not_ranked() -> None:
    labels = {
        "plumbing": ["Plumber", "Water Repair"],
        "electrical": ["Electrician"],
    }

    assert match_catalog_selector("  WATER   REPAIR ", labels) == "plumbing"
    assert match_catalog_selector("unknown", labels) is None

    with pytest.raises(CatalogSelectorAmbiguous):
        match_catalog_selector("repair", {"one": ["repair"], "two": ["repair"]})


def test_service_and_area_eligibility_is_deterministic() -> None:
    active_service = _service()
    validate_service_eligibility(active_service)

    with pytest.raises(ValueError, match="service_inactive"):
        validate_service_eligibility(_service(active=False))
    with pytest.raises(ValueError, match="service_not_bookable"):
        validate_service_eligibility(_service(bookable=False))

    active_area = ServiceAreaSpec(
        code="area",
        name_en="Area",
        name_ar="منطقة",
    )
    validate_service_area_eligibility(active_area)
    with pytest.raises(ValueError, match="unsupported_service_area"):
        validate_service_area_eligibility(active_area.model_copy(update={"active": False}))


def test_required_booking_details_allow_configured_values_only() -> None:
    service = _service().model_copy(
        update={
            "booking_requirements": [
                BookingRequirementSpec(
                    key="property_type",
                    label_en="Property type",
                    label_ar="نوع العقار",
                    required=True,
                ),
                BookingRequirementSpec(
                    key="unit_count",
                    label_en="Unit count",
                    label_ar="عدد الوحدات",
                    required=False,
                ),
            ]
        }
    )

    validate_required_booking_details(service, {"property_type": "villa"})
    with pytest.raises(ValueError, match="unknown requirement"):
        validate_required_booking_details(service, {"property_type": "villa", "secret": "x"})
    with pytest.raises(ValueError, match="required requirement"):
        validate_required_booking_details(service, {})
    with pytest.raises(ValueError, match="bounded"):
        validate_required_booking_details(service, {"property_type": "x" * 501})

    view = ServiceCatalogView(
        booking_requirements=(
            BookingRequirementSpec(
                key="property_type",
                label_en="Property type",
                label_ar="نوع العقار",
                required=True,
            ),
        )
    )
    validate_required_booking_details(view, {"property_type": "villa"})


def test_normalize_requested_interval_derives_duration_and_uses_utc() -> None:
    start_at = datetime(2026, 10, 11, 10, tzinfo=ZoneInfo("Asia/Riyadh"))
    interval = normalize_requested_interval(start_at, None, 90)

    assert interval.start_at_utc == datetime(2026, 10, 11, 7, tzinfo=UTC)
    assert interval.end_at_utc == datetime(2026, 10, 11, 8, 30, tzinfo=UTC)

    explicit = normalize_requested_interval(
        start_at,
        datetime(2026, 10, 11, 8, 45, tzinfo=UTC),
        90,
    )
    assert explicit.end_at_utc == datetime(2026, 10, 11, 8, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    "start_at,end_at,duration",
    [
        (datetime.fromisoformat("2026-10-11T10:00:00"), None, 60),
        (datetime(2026, 10, 11, 10, tzinfo=UTC), None, 0),
        (
            datetime(2026, 10, 11, 10, tzinfo=UTC),
            datetime(2026, 10, 11, 9, tzinfo=UTC),
            60,
        ),
        (
            datetime(2026, 10, 11, 10, tzinfo=UTC),
            datetime(2026, 10, 11, 10, tzinfo=UTC),
            60,
        ),
    ],
)
def test_normalize_requested_interval_rejects_invalid_values(
    start_at: datetime, end_at: datetime | None, duration: int
) -> None:
    with pytest.raises(ValueError):
        normalize_requested_interval(start_at, end_at, duration)


def test_normalize_requested_interval_rejects_nonexistent_and_ambiguous_wall_times() -> None:
    eastern = ZoneInfo("America/New_York")
    nonexistent = datetime(2026, 3, 8, 2, 30, tzinfo=eastern)
    ambiguous = datetime(2026, 11, 1, 1, 30, tzinfo=eastern)

    with pytest.raises(ValueError, match="nonexistent"):
        normalize_requested_interval(nonexistent, None, 60)
    with pytest.raises(ValueError, match="ambiguous"):
        normalize_requested_interval(ambiguous, None, 60)


def test_reference_timezone_conversion_is_not_hard_coded_into_normalization() -> None:
    start_at = datetime(2026, 10, 11, 10, tzinfo=ZoneInfo("Asia/Riyadh"))
    interval = normalize_requested_interval(start_at, None, 60)
    assert interval.start_at_utc == datetime(2026, 10, 11, 7, tzinfo=UTC)


def test_booking_policy_applies_weekly_hours_edges_buffers_notice_advance_and_slots() -> None:
    config = build_reference_business()
    sunday = date(2026, 10, 11)
    now_utc = datetime(2026, 10, 11, 3, 30, tzinfo=UTC)

    valid = evaluate_booking_policy(config, _interval_at(sunday, time(8, 30)), now_utc)
    assert valid.valid is True
    assert valid.error_code is None
    assert valid.effective_interval.start_at_utc == datetime(2026, 10, 11, 5, 15, tzinfo=UTC)

    before_open = evaluate_booking_policy(config, _interval_at(sunday, time(8, 0)), now_utc)
    assert before_open.error_code == "outside_business_policy"

    closing_edge = evaluate_booking_policy(config, _interval_at(sunday, time(18, 30), 75), now_utc)
    assert closing_edge.valid is True
    after_close = evaluate_booking_policy(config, _interval_at(sunday, time(19, 0), 75), now_utc)
    assert after_close.error_code == "outside_business_policy"

    friday = evaluate_booking_policy(
        config,
        _interval_at(date(2026, 10, 9), time(14, 30)),
        datetime(2026, 10, 9, 9, tzinfo=UTC),
    )
    assert friday.valid is True
    friday_before_open = evaluate_booking_policy(
        config,
        _interval_at(date(2026, 10, 9), time(13, 30)),
        datetime(2026, 10, 9, 9, tzinfo=UTC),
    )
    assert friday_before_open.error_code == "outside_business_policy"

    misaligned = evaluate_booking_policy(config, _interval_at(sunday, time(8, 15)), now_utc)
    assert misaligned.error_code == "outside_business_policy"

    too_soon = evaluate_booking_policy(
        config,
        _interval_at(sunday, time(8, 0)),
        datetime(2026, 10, 11, 4, 31, tzinfo=UTC),
    )
    assert too_soon.error_code == "outside_business_policy"

    too_far = evaluate_booking_policy(
        config,
        _interval_at(date(2026, 11, 11), time(8, 30)),
        datetime(2026, 10, 11, 6, 30, tzinfo=UTC),
    )
    assert too_far.error_code == "outside_business_policy"


def test_booking_policy_rejects_cross_local_day_and_naive_now() -> None:
    config = build_reference_business()
    start = datetime(2026, 10, 11, 20, 30, tzinfo=UTC)
    interval = normalize_requested_interval(start, start + timedelta(hours=2), 120)
    decision = evaluate_booking_policy(config, interval, datetime(2026, 10, 11, 6, tzinfo=UTC))
    assert decision.error_code == "outside_business_policy"

    with pytest.raises(ValueError):
        evaluate_booking_policy(
            config,
            _interval_at(date(2026, 10, 11), time(8, 30)),
            datetime.fromisoformat("2026-10-11T06:00:00"),
        )


def test_availability_decision_is_policy_and_provider_truth_with_half_open_edges() -> None:
    requested = RequestedInterval(
        datetime(2026, 10, 11, 7, tzinfo=UTC),
        datetime(2026, 10, 11, 8, tzinfo=UTC),
    )
    effective = RequestedInterval(
        datetime(2026, 10, 11, 6, 45, tzinfo=UTC),
        datetime(2026, 10, 11, 8, 15, tzinfo=UTC),
    )
    touching_before = CalendarInterval(
        datetime(2026, 10, 11, 6, tzinfo=UTC),
        datetime(2026, 10, 11, 6, 45, tzinfo=UTC),
    )
    touching_after = CalendarInterval(
        datetime(2026, 10, 11, 8, 15, tzinfo=UTC),
        datetime(2026, 10, 11, 9, tzinfo=UTC),
    )
    assert intervals_overlap(requested, touching_before) is False
    assert intervals_overlap(requested, touching_after) is False
    assert intervals_overlap(effective, touching_before) is False
    assert intervals_overlap(effective, touching_after) is False

    free = decide_availability(
        policy_valid=True,
        effective_interval=effective,
        provider_intervals=(touching_before, touching_after),
    )
    assert free == AvailabilityDecision(True, True, None, effective)

    blocking = CalendarInterval(
        datetime(2026, 10, 11, 8, tzinfo=UTC),
        datetime(2026, 10, 11, 8, 30, tzinfo=UTC),
    )
    busy = decide_availability(
        policy_valid=True,
        effective_interval=effective,
        provider_intervals=(blocking,),
    )
    assert busy == AvailabilityDecision(True, False, None, effective)

    invalid = decide_availability(
        policy_valid=False,
        effective_interval=effective,
        provider_intervals=(),
        policy_error_code="outside_business_policy",
    )
    assert invalid == AvailabilityDecision(False, False, "outside_business_policy", effective)


def test_booking_policy_uses_configured_timezone_for_dst_zone() -> None:
    timezone = "America/New_York"
    config = BusinessConfigView(
        timezone=timezone,
        weekly_hours=WeeklyHours(days={Weekday.SUNDAY: [TimeWindow(start=time(1), end=time(4))]}),
        booking_policy=BookingPolicySpec(
            minimum_notice_minutes=0,
            maximum_advance_days=30,
            buffer_before_minutes=0,
            buffer_after_minutes=0,
            slot_increment_minutes=30,
        ),
    )
    interval = normalize_requested_interval(
        datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        60,
    )
    decision = evaluate_booking_policy(config, interval, datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert decision.valid is True


def test_booking_snapshot_and_fingerprint_are_canonical_and_field_sensitive() -> None:
    values = {
        "booking_id": uuid4(),
        "service_id": uuid4(),
        "status": BookingStatus.CONFIRMED,
        "start_at_utc": datetime(2026, 10, 11, 7, tzinfo=UTC),
        "end_at_utc": datetime(2026, 10, 11, 8, tzinfo=UTC),
        "calendar_id": "calendar-id",
        "calendar_event_id": "event-id",
        "request_data": {"service_code": "plumbing", "area_code": "al_olaya"},
    }
    snapshot = canonical_booking_snapshot(**values)
    same_snapshot = canonical_booking_snapshot(**values)
    assert snapshot == same_snapshot
    assert booking_state_fingerprint(snapshot) == booking_state_fingerprint(same_snapshot)
    assert set(snapshot) == {
        "booking_id",
        "service_id",
        "status",
        "start_at_utc",
        "end_at_utc",
        "calendar_id",
        "calendar_event_id",
        "request_data_sha256",
    }

    changes = {
        "booking_id": uuid4(),
        "service_id": uuid4(),
        "status": BookingStatus.CANCELLED,
        "start_at_utc": datetime(2026, 10, 11, 8, tzinfo=UTC),
        "end_at_utc": datetime(2026, 10, 11, 9, tzinfo=UTC),
        "calendar_id": "other-calendar",
        "calendar_event_id": "other-event",
        "request_data": {"service_code": "electrical", "area_code": "al_malaz"},
    }
    for field, value in changes.items():
        changed = {**values, field: value}
        changed_snapshot = canonical_booking_snapshot(**changed)
        assert booking_state_fingerprint(changed_snapshot) != booking_state_fingerprint(snapshot)
