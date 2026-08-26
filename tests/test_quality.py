from orbital_signal.domain import AwardRecord, OrganizationType
from orbital_signal.quality import assess_signal_quality, classify_organization


def make_award(
    *,
    recipient_name: str,
    description: str,
    amount: float = 100_000,
) -> AwardRecord:
    return AwardRecord(
        source="usaspending",
        source_award_id=f"TEST-{recipient_name}",
        recipient_name=recipient_name,
        amount=amount,
        awarding_agency="National Aeronautics and Space Administration",
        description=description,
        source_url="https://www.usaspending.gov/award/TEST",
    )


def test_university_is_classified_as_academic() -> None:
    organization_type = classify_organization("California Institute of Technology")

    assert organization_type is OrganizationType.ACADEMIC_OR_RESEARCH


def test_applied_physics_lab_wins_over_llc_suffix() -> None:
    organization_type = classify_organization(
        "The Johns Hopkins University Applied Physics Laboratory LLC"
    )

    assert organization_type is OrganizationType.ACADEMIC_OR_RESEARCH


def test_company_with_technical_award_is_candidate() -> None:
    award = make_award(
        recipient_name="Phinyx Technologies, Inc.",
        description="Flight-ready generative design for rocket engine cooling systems",
    )

    assessment = assess_signal_quality(award)

    assert assessment.organization_type is OrganizationType.COMPANY
    assert assessment.is_startup_candidate is True
    assert assessment.quality_flags == []


def test_training_purchase_is_flagged() -> None:
    award = make_award(
        recipient_name="Instar Engineering and Consulting, Inc.",
        description="Spacecraft structures live instructor-led training",
    )

    assessment = assess_signal_quality(award)

    assert assessment.is_startup_candidate is False
    assert assessment.quality_flags == ["administrative_or_training_event"]


def test_tiny_service_modification_is_flagged() -> None:
    award = make_award(
        recipient_name="Trace Systems Inc.",
        description="Enhanced mobile satellite services equipment/activation/repair",
        amount=51,
    )

    assessment = assess_signal_quality(award)

    assert assessment.is_startup_candidate is False
    assert assessment.quality_flags == [
        "low_dollar_event",
        "administrative_or_training_event",
    ]
