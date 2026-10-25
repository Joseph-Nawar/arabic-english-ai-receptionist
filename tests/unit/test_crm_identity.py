from __future__ import annotations

from dataclasses import replace

import pytest

from receptionist.application.crm_identity import (
    CRMContactIdentityOutcome,
    decide_crm_contact_identity,
)
from receptionist.domain.identity import CRMContactLinkProvenance
from receptionist.integrations.hubspot import (
    HubSpotContactSearchResult,
    HubSpotContactSnapshot,
)

pytestmark = pytest.mark.unit


PHONE = "+966501234567"
EMAIL = "trusted@example.test"


def contact(
    contact_id: str = "101",
    *,
    email: str | None = None,
    phone: str | None = None,
    mobilephone: str | None = None,
    archived: bool = False,
) -> HubSpotContactSnapshot:
    return HubSpotContactSnapshot(
        id=contact_id,
        email=email,
        phone=phone,
        mobilephone=mobilephone,
        archived=archived,
    )


def search(
    *results: HubSpotContactSnapshot,
    has_more: bool = False,
    next_after: str | None = None,
) -> HubSpotContactSearchResult:
    return HubSpotContactSearchResult(tuple(results), has_more, next_after)


def decide(
    *,
    phone: str | None = PHONE,
    email: str | None = None,
    phone_search: HubSpotContactSearchResult | None = None,
    email_match: HubSpotContactSnapshot | None = None,
    email_lookup_complete: bool = True,
):
    return decide_crm_contact_identity(
        local_phone_e164=phone,
        trusted_verified_email=email,
        phone_search=phone_search,
        email_match=email_match,
        email_lookup_complete=email_lookup_complete,
        default_region="SA",
    )


def test_exact_e164_phone_match() -> None:
    result = decide(phone_search=search(contact(phone=PHONE)))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.hubspot_contact_id == "101"
    assert result.linked_by is CRMContactLinkProvenance.PHONE_E164


def test_provider_national_phone_is_normalized_with_business_region() -> None:
    result = decide(phone_search=search(contact(phone="0501234567")))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED


def test_nonmatching_phone_is_not_found() -> None:
    result = decide(phone_search=search(contact(phone="+966551234567")))

    assert result.outcome is CRMContactIdentityOutcome.NOT_FOUND


def test_invalid_provider_phone_does_not_become_a_match() -> None:
    result = decide(phone_search=search(contact(phone="not a phone")))

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_duplicate_candidates_with_same_provider_id_are_deduplicated() -> None:
    candidate = contact(phone=PHONE)
    result = decide(phone_search=search(candidate, candidate))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.hubspot_contact_id == "101"


def test_same_provider_id_with_inconsistent_candidate_data_is_ambiguous() -> None:
    result = decide(
        phone_search=search(
            contact(phone=PHONE),
            contact(phone=PHONE, email=EMAIL),
        )
    )

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_two_distinct_provider_contacts_matching_phone_are_ambiguous() -> None:
    result = decide(phone_search=search(contact("101", phone=PHONE), contact("202", phone=PHONE)))

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS
    assert result.hubspot_contact_id is None


@pytest.mark.parametrize(
    ("has_more", "next_after"),
    [(True, "cursor"), (False, "cursor")],
)
def test_additional_or_inconsistent_search_page_is_ambiguous(
    has_more: bool,
    next_after: str | None,
) -> None:
    result = decide(
        phone_search=search(contact(phone=PHONE), has_more=has_more, next_after=next_after)
    )

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_incomplete_phone_search_is_ambiguous_even_with_one_match() -> None:
    result = decide(phone_search=search(contact(phone=PHONE), has_more=True))

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_matching_verified_email_links_with_email_provenance() -> None:
    result = decide(phone=None, email=EMAIL, email_match=contact(email=EMAIL))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL


def test_email_comparison_is_case_insensitive_and_trimmed() -> None:
    result = decide(phone=None, email=f"  {EMAIL.upper()} ", email_match=contact(email=EMAIL))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED


def test_provider_email_absence_is_not_a_conflict_for_phone_match() -> None:
    result = decide(email=EMAIL, phone_search=search(contact(phone=PHONE)))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.linked_by is CRMContactLinkProvenance.PHONE_E164


def test_provider_phone_absence_is_not_a_conflict_for_email_match() -> None:
    result = decide(
        email=EMAIL,
        email_match=contact(email=EMAIL),
        phone_search=search(),
    )

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.linked_by is CRMContactLinkProvenance.VERIFIED_EMAIL


def test_blank_provider_phone_is_treated_as_missing() -> None:
    result = decide(
        email=EMAIL,
        email_match=contact(email=EMAIL, phone="  "),
        phone_search=search(),
    )

    assert result.outcome is CRMContactIdentityOutcome.MATCHED


def test_conflicting_nonblank_provider_email_blocks_phone_match() -> None:
    result = decide(
        email=EMAIL,
        phone_search=search(contact(email="other@example.test", phone=PHONE)),
    )

    assert result.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT
    assert result.hubspot_contact_id is None


def test_phone_and_email_matching_same_contact_returns_phone_provenance() -> None:
    candidate = contact(email=EMAIL, phone=PHONE)
    result = decide(
        email=EMAIL,
        phone_search=search(candidate),
        email_match=candidate,
    )

    assert result.outcome is CRMContactIdentityOutcome.MATCHED
    assert result.hubspot_contact_id == "101"
    assert result.linked_by is CRMContactLinkProvenance.PHONE_E164


def test_phone_and_email_matching_different_contacts_is_conflict() -> None:
    result = decide(
        email=EMAIL,
        phone_search=search(contact("101", phone=PHONE)),
        email_match=contact("202", email=EMAIL),
    )

    assert result.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT


def test_withheld_phone_can_resolve_from_trusted_email() -> None:
    result = decide(phone=None, email=EMAIL, email_match=contact(email=EMAIL))

    assert result.outcome is CRMContactIdentityOutcome.MATCHED


def test_no_trusted_identity_evidence_is_unresolved() -> None:
    result = decide(phone=None)

    assert result.outcome is CRMContactIdentityOutcome.UNRESOLVED_IDENTITY


def test_malformed_candidate_data_fails_closed_without_echoing_pii() -> None:
    sentinel = "private-customer-sentinel@example.test"
    malformed = replace(contact(phone=PHONE), id=" ", email=sentinel)
    result = decide(phone_search=search(malformed))

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS
    assert sentinel not in repr(result)


def test_customer_names_are_not_inputs_to_matching() -> None:
    result = decide(
        phone=None,
        email=None,
        email_match=contact(),
    )

    assert result.outcome is CRMContactIdentityOutcome.UNRESOLVED_IDENTITY


def test_local_phone_must_already_be_canonical_e164() -> None:
    result = decide(phone="+966 50 123 4567", phone_search=search(contact(phone=PHONE)))

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_incomplete_email_lookup_is_not_a_verified_no_match() -> None:
    result = decide(phone=None, email=EMAIL, email_lookup_complete=False)

    assert result.outcome is CRMContactIdentityOutcome.AMBIGUOUS


def test_email_match_with_conflicting_nonblank_provider_phone_is_conflict() -> None:
    result = decide(
        email=EMAIL,
        email_match=contact(email=EMAIL, phone="+966551234567"),
        phone_search=search(),
    )

    assert result.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT


def test_returned_email_lookup_with_different_nonblank_email_is_conflict() -> None:
    result = decide(
        phone=None,
        email=EMAIL,
        email_match=contact(email="other@example.test"),
    )

    assert result.outcome is CRMContactIdentityOutcome.IDENTITY_CONFLICT


def test_decision_has_no_customer_pii_or_provider_data() -> None:
    result = decide(
        phone=None,
        email=EMAIL,
        email_match=contact(email=EMAIL),
    )

    assert EMAIL not in repr(result)
    assert PHONE not in repr(result)
    assert result.hubspot_contact_id == "101"
