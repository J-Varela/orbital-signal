from datetime import date

from orbital_signal.domain import AwardRecord
from orbital_signal.relevance import assess_space_relevance


def test_strong_terms_make_award_relevant(satellite_award: AwardRecord) -> None:
    assessment = assess_space_relevance(satellite_award)

    assert assessment.is_space_relevant is True
    assert assessment.score == 12
    assert assessment.matched_terms == ["payload", "satellite", "space domain awareness"]


def test_nasa_agency_alone_does_not_cross_threshold() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="ADMIN-001",
        recipient_name="Example Office Supply Company",
        amount=10_000,
        awarding_agency="National Aeronautics and Space Administration",
        description="Office furniture and administrative supplies.",
        start_date=date(2026, 8, 1),
        source_url="https://www.usaspending.gov/award/ADMIN-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.is_space_relevant is False
    assert assessment.score == 3


def test_space_naics_code_is_independently_relevant() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="MFG-001",
        recipient_name="Example Manufacturing",
        amount=1_000_000,
        awarding_agency="Department of Defense",
        description="Advanced vehicle components.",
        naics_code="336414",
        source_url="https://www.usaspending.gov/award/MFG-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.is_space_relevant is True
    assert assessment.score == 5


def test_office_space_does_not_match_generic_space_term() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="LEASE-001",
        recipient_name="Example Property Company",
        amount=500_000,
        awarding_agency="General Services Administration",
        description="Lease of office space and parking.",
        source_url="https://www.usaspending.gov/award/LEASE-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.is_space_relevant is False
    assert assessment.score == 0


def test_orbital_welding_does_not_mean_orbital_spaceflight() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="WELD-001",
        recipient_name="Peck Enterprises LLC",
        amount=29_500,
        awarding_agency="Department of Defense",
        description="ASME Section IX orbital welding training",
        source_url="https://www.usaspending.gov/award/WELD-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.is_space_relevant is False
    assert assessment.score == 0
