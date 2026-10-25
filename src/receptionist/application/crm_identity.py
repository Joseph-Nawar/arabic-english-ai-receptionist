"""Deterministic, side-effect-free HubSpot Contact identity decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from receptionist.domain.identity import (
    CRMContactLinkProvenance,
    PhoneNormalizationError,
    normalize_phone_number,
)
from receptionist.integrations.hubspot import (
    HubSpotContactSearchResult,
    HubSpotContactSnapshot,
)

_MAX_PROVIDER_ID_LENGTH = 255
_MAX_EMAIL_LENGTH = 320
_MAX_PHONE_LENGTH = 128
_MAX_PHONE_CANDIDATES = 20


class CRMContactIdentityOutcome(StrEnum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    IDENTITY_CONFLICT = "identity_conflict"
    UNRESOLVED_IDENTITY = "unresolved_identity"


@dataclass(frozen=True, slots=True)
class CRMContactIdentityDecision:
    """Safe identity result; it deliberately contains no customer properties."""

    outcome: CRMContactIdentityOutcome
    hubspot_contact_id: str | None = None
    linked_by: CRMContactLinkProvenance | None = None


def decide_crm_contact_identity(
    *,
    local_phone_e164: str | None,
    trusted_verified_email: str | None,
    phone_search: HubSpotContactSearchResult | None,
    email_match: HubSpotContactSnapshot | None,
    email_lookup_complete: bool,
    default_region: str,
) -> CRMContactIdentityDecision:
    """Decide whether supplied trusted evidence identifies exactly one Contact.

    ``phone_search`` must be a complete bounded search when a local phone is
    supplied. ``email_match`` is the completed exact-email lookup result. The
    caller remains responsible for translating provider failures into incomplete
    evidence; this function never turns incomplete results into a no-match.
    """
    phone = _canonical_local_phone(local_phone_e164, default_region)
    email = _normalized_email(trusted_verified_email)
    if phone is _INVALID or email is _INVALID:
        return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
    if phone is None and email is None:
        return _decision(CRMContactIdentityOutcome.UNRESOLVED_IDENTITY)

    search_result: HubSpotContactSearchResult | None = None
    if phone is not None:
        if not isinstance(phone_search, HubSpotContactSearchResult) or not _valid_search(
            phone_search
        ):
            return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
        search_result = phone_search
        if not search_result.complete or len(search_result.results) > _MAX_PHONE_CANDIDATES:
            return _decision(CRMContactIdentityOutcome.AMBIGUOUS)

    if email is not None and not email_lookup_complete:
        return _decision(CRMContactIdentityOutcome.AMBIGUOUS)

    snapshots: dict[str, HubSpotContactSnapshot] = {}
    if phone is not None:
        if search_result is None:
            return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
        for candidate in search_result.results:
            if not _valid_snapshot(candidate):
                return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
            prior = snapshots.get(candidate.id)
            if prior is not None and prior != candidate:
                return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
            snapshots[candidate.id] = candidate

    if email_match is not None:
        if not _valid_snapshot(email_match):
            return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
        prior = snapshots.get(email_match.id)
        if prior is not None and prior != email_match:
            return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
        snapshots[email_match.id] = email_match

    phone_matches: set[str] = set()
    email_matches: set[str] = set()
    conflicts: set[str] = set()
    malformed_matching_data = False

    for contact_id, candidate in snapshots.items():
        provider_email = _normalized_email(candidate.email)
        provider_phones = (
            _normalized_phone(candidate.phone, default_region),
            _normalized_phone(candidate.mobilephone, default_region),
        )
        if provider_email is _INVALID or _INVALID in provider_phones:
            malformed_matching_data = True
            continue

        candidate_phone_matches = phone is not None and phone in provider_phones
        candidate_email_matches = email is not None and provider_email == email

        if candidate_phone_matches:
            phone_matches.add(contact_id)
        if candidate_email_matches:
            email_matches.add(contact_id)

        if (
            email is not None
            and email_match is not None
            and contact_id == email_match.id
            and provider_email is not None
            and provider_email != email
        ):
            conflicts.add(contact_id)

        if candidate_phone_matches or candidate_email_matches:
            if candidate.archived:
                malformed_matching_data = True
                continue
            if phone is not None and any(
                value is not None and value != phone for value in provider_phones
            ):
                conflicts.add(contact_id)
            if email is not None and provider_email is not None and provider_email != email:
                conflicts.add(contact_id)

    if malformed_matching_data:
        return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
    if len(phone_matches) > 1 or len(email_matches) > 1:
        return _decision(CRMContactIdentityOutcome.AMBIGUOUS)
    if conflicts:
        return _decision(CRMContactIdentityOutcome.IDENTITY_CONFLICT)
    if phone_matches and email_matches and phone_matches != email_matches:
        return _decision(CRMContactIdentityOutcome.IDENTITY_CONFLICT)

    matched_ids = phone_matches | email_matches
    if not matched_ids:
        return _decision(CRMContactIdentityOutcome.NOT_FOUND)
    if len(matched_ids) != 1:
        return _decision(CRMContactIdentityOutcome.AMBIGUOUS)

    matched_id = next(iter(matched_ids))
    provenance = (
        CRMContactLinkProvenance.PHONE_E164
        if matched_id in phone_matches
        else CRMContactLinkProvenance.VERIFIED_EMAIL
    )
    return CRMContactIdentityDecision(
        outcome=CRMContactIdentityOutcome.MATCHED,
        hubspot_contact_id=matched_id,
        linked_by=provenance,
    )


class _InvalidEvidence:
    pass


_INVALID = _InvalidEvidence()


def _decision(outcome: CRMContactIdentityOutcome) -> CRMContactIdentityDecision:
    return CRMContactIdentityDecision(outcome=outcome)


def _canonical_local_phone(value: str | None, default_region: str) -> str | _InvalidEvidence | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID
    if not value.strip():
        return None
    normalized = _normalized_phone(value, default_region)
    if normalized is _INVALID or normalized != value:
        return _INVALID
    return normalized


def _normalized_phone(value: object, default_region: str) -> str | _InvalidEvidence | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _MAX_PHONE_LENGTH:
        return _INVALID
    if not value.strip():
        return None
    try:
        return normalize_phone_number(value, default_region)
    except PhoneNormalizationError:
        return _INVALID


def _normalized_email(value: object) -> str | _InvalidEvidence | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID
    normalized = value.strip().casefold()
    if not normalized:
        return None
    if (
        len(normalized) > _MAX_EMAIL_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in normalized)
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
    ):
        return _INVALID
    return normalized


def _valid_search(value: object) -> bool:
    if not isinstance(value, HubSpotContactSearchResult):
        return False
    if not isinstance(value.results, tuple) or not isinstance(value.has_more, bool):
        return False
    if value.next_after is not None and (
        not isinstance(value.next_after, str)
        or not value.next_after.strip()
        or len(value.next_after) > 255
    ):
        return False
    return (value.has_more and value.next_after is not None) or (
        not value.has_more and value.next_after is None
    )


def _valid_snapshot(value: object) -> bool:
    if not isinstance(value, HubSpotContactSnapshot):
        return False
    if (
        not isinstance(value.id, str)
        or not value.id.strip()
        or value.id != value.id.strip()
        or len(value.id) > _MAX_PROVIDER_ID_LENGTH
        or not isinstance(value.archived, bool)
    ):
        return False
    for property_value, max_length in (
        (value.email, _MAX_EMAIL_LENGTH),
        (value.phone, _MAX_PHONE_LENGTH),
        (value.mobilephone, _MAX_PHONE_LENGTH),
    ):
        if property_value is not None and (
            not isinstance(property_value, str) or len(property_value) > max_length
        ):
            return False
    return True
