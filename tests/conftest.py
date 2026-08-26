from datetime import date

import pytest

from orbital_signal.domain import AwardRecord


@pytest.fixture
def satellite_award() -> AwardRecord:
    return AwardRecord(
        source="usaspending",
        source_award_id="FAKE-001",
        generated_internal_id="CONT_AWD_FAKE-001_9700_-NONE-_-NONE-",
        recipient_name="Example Orbital Systems, Inc.",
        recipient_uei="EXAMPLE123",
        amount=4_800_000,
        awarding_agency="Department of Defense",
        description="Prototype satellite payload for space domain awareness.",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 8, 1),
        source_url="https://www.usaspending.gov/award/CONT_AWD_FAKE-001",
    )
