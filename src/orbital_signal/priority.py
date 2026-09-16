"""Deterministic priority scoring for space opportunity signals."""

from datetime import date

from pydantic import BaseModel, Field

from orbital_signal.domain import AwardRecord


class OpportunityPriorityAssessment(BaseModel):
    """Explainable priority score for a space opportunity signal."""

    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


ADVANCED_TECH_TERMS = (
    "autonomous",
    "autonomy",
    "robotics",
    "navigation",
    "guidance",
    "avionics",
    "propulsion",
    "sensing",
    "sensor",
)


def assess_opportunity_priority(
    award: AwardRecord,
    *,
    relevance_score: int,
    is_startup_candidate: bool,
    as_of: date,
) -> OpportunityPriorityAssessment:
    """Score how much attention a space signal deserves right now."""

    score = 0
    reasons: list[str] = []

    relevance_points = min(relevance_score * 4, 40)
    score += relevance_points
    if relevance_points:
        reasons.append(f"space relevance (+{relevance_points})")

    if award.action_date is not None:
        age_days = max((as_of - award.action_date).days, 0)

        if age_days <= 7:
            score += 25
            reasons.append("recent transaction within 7 days (+25)")
        elif age_days <= 30:
            score += 18
            reasons.append("recent transaction within 30 days (+18)")
        elif age_days <= 90:
            score += 10
            reasons.append("recent transaction within 90 days (+10)")

    if is_startup_candidate:
        score += 15
        reasons.append("startup candidate (+15)")

    if award.amount >= 10_000_000:
        score += 10
        reasons.append("award value at least $10M (+10)")
    elif award.amount >= 1_000_000:
        score += 8
        reasons.append("award value at least $1M (+8)")
    elif award.amount >= 250_000:
        score += 5
        reasons.append("award value at least $250K (+5)")
    elif award.amount >= 25_000:
        score += 2
        reasons.append("award value at least $25K (+2)")

    normalized_description = award.description.casefold()
    matched_advanced_terms = [
        term for term in ADVANCED_TECH_TERMS if term in normalized_description
    ]

    advanced_points = min(len(matched_advanced_terms) * 2, 10)
    if advanced_points:
        score += advanced_points
        reasons.append(f"advanced technology evidence (+{advanced_points})")

    return OpportunityPriorityAssessment(
        score=min(score, 100),
        reasons=reasons,
    )
