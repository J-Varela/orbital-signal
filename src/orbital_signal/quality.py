"""Recipient and event-quality rules for the startup candidate feed."""

import re

from orbital_signal.domain import (
    AwardRecord,
    OrganizationType,
    SignalQualityAssessment,
)

ACADEMIC_OR_RESEARCH_PATTERNS = (
    "university",
    "college",
    "institute of technology",
    "applied physics laboratory",
    "space dynamics laboratory",
)

GOVERNMENT_OR_NONPROFIT_PATTERNS = (
    "national laboratory",
    "research foundation",
    "government",
    "county of",
    "city of",
)

COMPANY_SUFFIX_PATTERN = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|company|co)\.?\b",
    flags=re.IGNORECASE,
)

ADMINISTRATIVE_OR_SERVICE_PHRASES = (
    "instructor-led training",
    "orbital welding training",
    "rental of exhibit space",
    "conference registration",
    "equipment/activation/repair",
    "office furniture",
    "administrative supplies",
)

DEFAULT_MINIMUM_CANDIDATE_AMOUNT = 25_000


def classify_organization(recipient_name: str) -> OrganizationType:
    """Infer a broad recipient type without claiming verified startup status."""

    normalized_name = recipient_name.casefold()
    if any(pattern in normalized_name for pattern in ACADEMIC_OR_RESEARCH_PATTERNS):
        return OrganizationType.ACADEMIC_OR_RESEARCH
    if any(pattern in normalized_name for pattern in GOVERNMENT_OR_NONPROFIT_PATTERNS):
        return OrganizationType.GOVERNMENT_OR_NONPROFIT
    if COMPANY_SUFFIX_PATTERN.search(recipient_name):
        return OrganizationType.COMPANY
    return OrganizationType.UNKNOWN


def assess_signal_quality(
    award: AwardRecord,
    *,
    minimum_candidate_amount: float = DEFAULT_MINIMUM_CANDIDATE_AMOUNT,
) -> SignalQualityAssessment:
    """Determine whether an event belongs in the startup-candidate view."""

    organization_type = classify_organization(award.recipient_name)
    normalized_description = award.description.casefold()
    quality_flags: list[str] = []

    if organization_type is not OrganizationType.COMPANY:
        quality_flags.append(f"recipient_type:{organization_type.value}")

    if award.amount < minimum_candidate_amount:
        quality_flags.append("low_dollar_event")

    if any(phrase in normalized_description for phrase in ADMINISTRATIVE_OR_SERVICE_PHRASES):
        quality_flags.append("administrative_or_training_event")

    return SignalQualityAssessment(
        organization_type=organization_type,
        is_startup_candidate=(organization_type is OrganizationType.COMPANY and not quality_flags),
        quality_flags=quality_flags,
    )
