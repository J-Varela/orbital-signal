from datetime import date

from orbital_signal.domain import AwardRecord
from orbital_signal.relevance import assess_space_relevance


def test_strong_terms_make_award_relevant(satellite_award: AwardRecord) -> None:
    assessment = assess_space_relevance(satellite_award)

    assert assessment.is_space_relevant is True
    assert assessment.score == 10
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


def test_generic_satellite_mention_is_not_high_confidence() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="SAT-001",
        recipient_name="Generic Contractor",
        amount=200_000,
        awarding_agency="Department of Defense",
        description="General satellite support services.",
        source_url="https://example.test/SAT-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.score < 6


def test_lunar_autonomy_scores_above_generic_satellite_support() -> None:
    generic = AwardRecord(
        source="usaspending",
        source_award_id="SAT-001",
        recipient_name="Generic Contractor",
        amount=200_000,
        awarding_agency="Department of Defense",
        description="General satellite support services.",
        source_url="https://example.test/SAT-001",
    )

    advanced = AwardRecord(
        source="usaspending",
        source_award_id="LUNAR-001",
        recipient_name="Advanced Space Systems",
        amount=2_000_000,
        awarding_agency="National Aeronautics and Space Administration",
        description=("Autonomous navigation and guidance system for lunar spacecraft."),
        source_url="https://example.test/LUNAR-001",
    )

    generic_score = assess_space_relevance(generic).score
    advanced_score = assess_space_relevance(advanced).score

    assert advanced_score > generic_score


def test_payload_term_alone_is_not_treated_as_strong_space_signal() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="PAYLOAD-001",
        recipient_name="Example Logistics",
        amount=50_000,
        awarding_agency="Department of Defense",
        description="Payload transport and handling support.",
        source_url="https://example.test/PAYLOAD-001",
    )

    assessment = assess_space_relevance(award)

    assert assessment.is_space_relevant is False
