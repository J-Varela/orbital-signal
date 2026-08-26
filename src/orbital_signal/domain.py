"""Core domain models used across sources, scoring, and the API."""

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class AwardRecord(BaseModel):
    """A source-independent representation of a prime federal award."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source: str
    source_award_id: str
    generated_internal_id: str | None = None
    recipient_name: str
    recipient_uei: str | None = None
    amount: float = Field(ge=0)
    awarding_agency: str
    description: str = ""
    start_date: date | None = None
    end_date: date | None = None
    naics_code: str | None = None
    psc_code: str | None = None
    source_url: HttpUrl

    @field_validator("recipient_name", "source_award_id", "awarding_agency")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


class RelevanceAssessment(BaseModel):
    """Explainable output from the deterministic relevance engine."""

    score: int = Field(ge=0, le=100)
    is_space_relevant: bool
    matched_terms: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class OrganizationType(StrEnum):
    """Coarse recipient type inferred from public award evidence."""

    COMPANY = "company"
    ACADEMIC_OR_RESEARCH = "academic_or_research"
    GOVERNMENT_OR_NONPROFIT = "government_or_nonprofit"
    UNKNOWN = "unknown"


class SignalQualityAssessment(BaseModel):
    """Startup-feed eligibility kept separate from space relevance."""

    organization_type: OrganizationType
    is_startup_candidate: bool
    quality_flags: list[str] = Field(default_factory=list)


class CompanySignal(BaseModel):
    """A space-relevant event tied to a recipient company."""

    signal_id: str
    company_name: str
    company_uei: str | None = None
    signal_type: str = "government_award"
    occurred_on: date | None = None
    amount: float = Field(ge=0)
    agency: str
    summary: str
    relevance_score: int = Field(ge=0, le=100)
    matched_terms: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    organization_type: OrganizationType = OrganizationType.UNKNOWN
    is_startup_candidate: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    source: str
    source_award_id: str
    evidence_url: HttpUrl
    detected_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )


class IngestionResult(BaseModel):
    """Summary of one source ingestion run."""

    source: str
    fetched_count: int
    relevant_count: int
    stored_count: int
    duplicate_count: int
