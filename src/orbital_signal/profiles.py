"""Company-level intelligence aggregation."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from orbital_signal.domain import (
    CompanyIntelligenceProfile,
    CompanySignal,
)

ESTABLISHED_CONTRACTOR_FLAG = "established_contractor"


def build_company_profile(
    signals: Iterable[CompanySignal],
) -> CompanyIntelligenceProfile:
    """Aggregate intelligence signals into one company profile."""

    company_signals = list(signals)
    if not company_signals:
        raise ValueError("at least one company signal is required")

    company_ueis = {
        signal.company_uei for signal in company_signals if signal.company_uei is not None
    }

    if len(company_ueis) > 1:
        raise ValueError("signals contain multiple company UEIs")

    canonical_signal = max(
        company_signals,
        key=lambda signal: (
            signal.occurred_on or date.min,
            signal.signal_id,
        ),
    )

    agencies = sorted(
        {signal.agency for signal in company_signals},
        key=str.casefold,
    )

    occurred_dates = [
        signal.occurred_on for signal in company_signals if signal.occurred_on is not None
    ]

    established_contractor = any(
        ESTABLISHED_CONTRACTOR_FLAG in signal.quality_flags for signal in company_signals
    )

    startup_candidate = (
        any(signal.is_startup_candidate for signal in company_signals)
        and not established_contractor
    )

    average_relevance_score = sum(signal.relevance_score for signal in company_signals) / len(
        company_signals
    )

    return CompanyIntelligenceProfile(
        company_name=canonical_signal.company_name,
        company_uei=next(iter(company_ueis), None),
        signal_count=len(company_signals),
        total_amount=sum(signal.amount for signal in company_signals),
        agencies=agencies,
        latest_signal_date=max(occurred_dates) if occurred_dates else None,
        average_relevance_score=round(average_relevance_score, 2),
        max_priority_score=max(signal.priority_score for signal in company_signals),
        startup_candidate=startup_candidate,
        established_contractor=established_contractor,
    )


def build_company_profiles(
    signals: Iterable[CompanySignal],
) -> list[CompanyIntelligenceProfile]:
    """Group signals by company and return ranked company profiles."""

    grouped: dict[str, list[CompanySignal]] = defaultdict(list)

    for signal in signals:
        grouped[_company_identity(signal)].append(signal)

    profiles = [build_company_profile(company_signals) for company_signals in grouped.values()]

    return sorted(
        profiles,
        key=lambda profile: (
            profile.max_priority_score,
            profile.average_relevance_score,
            profile.total_amount,
            profile.company_name.casefold(),
        ),
        reverse=True,
    )


def _company_identity(signal: CompanySignal) -> str:
    """Prefer UEI identity, falling back to normalized company name."""

    if signal.company_uei:
        return f"uei:{signal.company_uei.casefold()}"

    return f"name:{signal.company_name.casefold().strip()}"
