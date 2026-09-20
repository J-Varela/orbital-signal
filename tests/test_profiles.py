from datetime import date

import pytest

from orbital_signal.domain import CompanySignal
from orbital_signal.profiles import build_company_profile, build_company_profiles


def make_signal(
    *,
    signal_id: str,
    company_name: str = "Example Orbital Systems, Inc.",
    company_uei: str | None = "EXAMPLE123",
    occurred_on: date | None = date(2026, 8, 20),
    amount: float = 1_000_000,
    agency: str = "NASA",
    relevance_score: int = 80,
    priority_score: int = 70,
    is_startup_candidate: bool = True,
    quality_flags: list[str] | None = None,
) -> CompanySignal:
    return CompanySignal(
        signal_id=signal_id,
        company_name=company_name,
        company_uei=company_uei,
        occurred_on=occurred_on,
        amount=amount,
        agency=agency,
        summary="Space technology development.",
        relevance_score=relevance_score,
        priority_score=priority_score,
        is_startup_candidate=is_startup_candidate,
        quality_flags=quality_flags or [],
        source="usaspending",
        source_award_id=f"AWARD-{signal_id}",
        evidence_url=f"https://www.usaspending.gov/award/{signal_id}",
    )


def test_build_company_profile_aggregates_multiple_signals() -> None:
    signals = [
        make_signal(
            signal_id="001",
            amount=1_000_000,
            relevance_score=80,
            priority_score=65,
        ),
        make_signal(
            signal_id="002",
            amount=4_000_000,
            relevance_score=90,
            priority_score=88,
        ),
    ]

    profile = build_company_profile(signals)

    assert profile.company_name == "Example Orbital Systems, Inc."
    assert profile.company_uei == "EXAMPLE123"
    assert profile.signal_count == 2
    assert profile.total_amount == 5_000_000
    assert profile.average_relevance_score == 85
    assert profile.max_priority_score == 88


def test_company_profile_deduplicates_agencies() -> None:
    signals = [
        make_signal(signal_id="001", agency="NASA"),
        make_signal(signal_id="002", agency="NASA"),
        make_signal(signal_id="003", agency="U.S. Space Force"),
    ]

    profile = build_company_profile(signals)

    assert profile.agencies == ["NASA", "U.S. Space Force"]


def test_company_profile_tracks_latest_signal_date() -> None:
    signals = [
        make_signal(signal_id="001", occurred_on=date(2026, 6, 1)),
        make_signal(signal_id="002", occurred_on=date(2026, 9, 12)),
        make_signal(signal_id="003", occurred_on=None),
    ]

    profile = build_company_profile(signals)

    assert profile.latest_signal_date == date(2026, 9, 12)


def test_company_profile_preserves_startup_candidacy() -> None:
    signals = [
        make_signal(
            signal_id="001",
            is_startup_candidate=False,
            quality_flags=["low_dollar_event"],
        ),
        make_signal(
            signal_id="002",
            is_startup_candidate=True,
        ),
    ]

    profile = build_company_profile(signals)

    assert profile.startup_candidate is True
    assert profile.established_contractor is False


def test_established_contractor_overrides_startup_candidacy() -> None:
    signals = [
        make_signal(
            signal_id="001",
            company_name="Amentum Technology, Inc.",
            is_startup_candidate=False,
            quality_flags=["established_contractor"],
        ),
        make_signal(
            signal_id="002",
            company_name="Amentum Technology, Inc.",
            is_startup_candidate=True,
        ),
    ]

    profile = build_company_profile(signals)

    assert profile.established_contractor is True
    assert profile.startup_candidate is False


def test_company_profiles_group_by_uei() -> None:
    signals = [
        make_signal(
            signal_id="001",
            company_name="Example Orbital Systems, Inc.",
            company_uei="SAME-UEI",
        ),
        make_signal(
            signal_id="002",
            company_name="Example Orbital Systems",
            company_uei="SAME-UEI",
        ),
    ]

    profiles = build_company_profiles(signals)

    assert len(profiles) == 1
    assert profiles[0].signal_count == 2
    assert profiles[0].company_uei == "SAME-UEI"


def test_company_profiles_fall_back_to_normalized_name() -> None:
    signals = [
        make_signal(
            signal_id="001",
            company_name="Example Orbital Systems",
            company_uei=None,
        ),
        make_signal(
            signal_id="002",
            company_name=" example orbital systems ",
            company_uei=None,
        ),
    ]

    profiles = build_company_profiles(signals)

    assert len(profiles) == 1
    assert profiles[0].signal_count == 2


def test_company_profiles_rank_highest_priority_first() -> None:
    lower_priority = make_signal(
        signal_id="001",
        company_name="Lower Priority Space, Inc.",
        company_uei="LOWER",
        priority_score=60,
    )
    higher_priority = make_signal(
        signal_id="002",
        company_name="Higher Priority Space, Inc.",
        company_uei="HIGHER",
        priority_score=95,
    )

    profiles = build_company_profiles([lower_priority, higher_priority])

    assert [profile.company_name for profile in profiles] == [
        "Higher Priority Space, Inc.",
        "Lower Priority Space, Inc.",
    ]


def test_company_profile_requires_at_least_one_signal() -> None:
    with pytest.raises(
        ValueError,
        match="at least one company signal is required",
    ):
        build_company_profile([])


def test_company_profile_rejects_multiple_ueis() -> None:
    signals = [
        make_signal(signal_id="001", company_uei="UEI-A"),
        make_signal(signal_id="002", company_uei="UEI-B"),
    ]

    with pytest.raises(
        ValueError,
        match="signals contain multiple company UEIs",
    ):
        build_company_profile(signals)
