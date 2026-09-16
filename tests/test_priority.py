from datetime import date

from orbital_signal.domain import AwardRecord
from orbital_signal.priority import assess_opportunity_priority


def make_award(
    *,
    amount: float,
    action_date: date | None,
    description: str,
) -> AwardRecord:
    return AwardRecord(
        source="usaspending",
        source_award_id="PRIORITY-001",
        recipient_name="Example Space Systems, Inc.",
        amount=amount,
        awarding_agency="National Aeronautics and Space Administration",
        description=description,
        action_date=action_date,
        source_url="https://www.usaspending.gov/award/PRIORITY-001",
    )


def test_recent_advanced_startup_signal_scores_high() -> None:
    award = make_award(
        amount=2_000_000,
        action_date=date(2026, 9, 10),
        description="Autonomous navigation system for lunar spacecraft.",
    )

    assessment = assess_opportunity_priority(
        award,
        relevance_score=10,
        is_startup_candidate=True,
        as_of=date(2026, 9, 15),
    )

    assert assessment.score == 92
    assert "recent transaction within 7 days (+25)" in assessment.reasons
    assert "startup candidate (+15)" in assessment.reasons


def test_old_generic_signal_scores_below_recent_technical_signal() -> None:
    old_award = make_award(
        amount=100_000,
        action_date=date(2026, 5, 1),
        description="General satellite support services.",
    )
    recent_award = make_award(
        amount=2_000_000,
        action_date=date(2026, 9, 10),
        description="Autonomous navigation system for lunar spacecraft.",
    )

    old = assess_opportunity_priority(
        old_award,
        relevance_score=4,
        is_startup_candidate=False,
        as_of=date(2026, 9, 15),
    )
    recent = assess_opportunity_priority(
        recent_award,
        relevance_score=10,
        is_startup_candidate=True,
        as_of=date(2026, 9, 15),
    )

    assert recent.score > old.score


def test_missing_action_date_gets_no_recency_bonus() -> None:
    award = make_award(
        amount=250_000,
        action_date=None,
        description="Spacecraft propulsion development.",
    )

    assessment = assess_opportunity_priority(
        award,
        relevance_score=8,
        is_startup_candidate=True,
        as_of=date(2026, 9, 15),
    )

    assert not any("recent transaction" in reason for reason in assessment.reasons)


def test_priority_score_is_capped_at_100() -> None:
    award = make_award(
        amount=100_000_000,
        action_date=date(2026, 9, 15),
        description=(
            "Autonomous robotics navigation guidance avionics and propulsion for lunar spacecraft."
        ),
    )

    assessment = assess_opportunity_priority(
        award,
        relevance_score=100,
        is_startup_candidate=True,
        as_of=date(2026, 9, 15),
    )

    assert assessment.score == 100
