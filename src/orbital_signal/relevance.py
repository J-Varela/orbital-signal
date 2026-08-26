"""Explainable rules for identifying space-related federal awards."""

import re
from dataclasses import dataclass

from orbital_signal.domain import AwardRecord, RelevanceAssessment


@dataclass(frozen=True)
class WeightedTerm:
    phrase: str
    points: int


STRONG_TERMS = (
    WeightedTerm("spacecraft", 4),
    WeightedTerm("satellite", 4),
    WeightedTerm("orbital", 4),
    WeightedTerm("launch vehicle", 4),
    WeightedTerm("rocket engine", 4),
    WeightedTerm("space domain awareness", 4),
    WeightedTerm("space force", 4),
    WeightedTerm("lunar", 4),
    WeightedTerm("cislunar", 4),
    WeightedTerm("missile warning", 4),
    WeightedTerm("payload", 4),
)

SUPPORTING_TERMS = (
    WeightedTerm("aerospace", 2),
    WeightedTerm("propulsion", 2),
    WeightedTerm("telemetry", 2),
    WeightedTerm("remote sensing", 2),
    WeightedTerm("ground station", 2),
    WeightedTerm("mission control", 2),
    WeightedTerm("hypersonic", 2),
    WeightedTerm("in-space", 2),
)

SPACE_AGENCY_PATTERNS = (
    "national aeronautics and space administration",
    "nasa",
    "space force",
    "space systems command",
    "space development agency",
)

SPACE_NAICS_CODES = {"336414", "336415", "336419"}
SPACE_PSC_PREFIXES = ("18", "AR")


def _contains_phrase(text: str, phrase: str) -> bool:
    pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def assess_space_relevance(
    award: AwardRecord,
    *,
    threshold: int = 4,
) -> RelevanceAssessment:
    """Score an award and preserve all evidence used to reach the result."""

    searchable_text = " ".join((award.description, award.awarding_agency)).lower()
    score = 0
    matched_terms: list[str] = []
    reasons: list[str] = []

    agency_matches = [
        agency
        for agency in SPACE_AGENCY_PATTERNS
        if _contains_phrase(award.awarding_agency, agency)
    ]
    if agency_matches:
        score += 3
        reasons.append(f"space-focused awarding organization: {agency_matches[0]}")

    for weighted_term in (*STRONG_TERMS, *SUPPORTING_TERMS):
        if weighted_term.phrase == "orbital" and "orbital welding" in searchable_text:
            continue
        if _contains_phrase(searchable_text, weighted_term.phrase):
            score += weighted_term.points
            matched_terms.append(weighted_term.phrase)
            reasons.append(f"matched term '{weighted_term.phrase}' (+{weighted_term.points})")

    if award.naics_code in SPACE_NAICS_CODES:
        score += 5
        reasons.append(f"space manufacturing NAICS {award.naics_code} (+5)")

    if award.psc_code and award.psc_code.upper().startswith(SPACE_PSC_PREFIXES):
        score += 4
        reasons.append(f"space-related PSC {award.psc_code} (+4)")

    bounded_score = min(score, 100)
    return RelevanceAssessment(
        score=bounded_score,
        is_space_relevant=bounded_score >= threshold,
        matched_terms=sorted(set(matched_terms)),
        reasons=reasons,
    )
