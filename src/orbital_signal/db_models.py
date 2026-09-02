"""Persistent SQLAlchemy models for companies, awards, and signals."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbital_signal.database import Base


class TimestampMixin:
    """Created and updated timestamps shared by mutable records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Company(TimestampMixin, Base):
    """Canonical identity for one tracked organization."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        unique=True,
    )
    uei: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        unique=True,
    )
    organization_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )

    aliases: Mapped[list[CompanyAlias]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    awards: Mapped[list[Award]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    signals: Mapped[list[Signal]] = relationship(
        back_populates="company",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CompanyAlias(TimestampMixin, Base):
    """Source-specific name connected to a canonical company."""

    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "normalized_alias",
            "source",
        ),
        Index(
            "ix_company_aliases_normalized_alias",
            "normalized_alias",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    alias: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    company: Mapped[Company] = relationship(back_populates="aliases")


class Award(TimestampMixin, Base):
    """Normalized government award preserved as source evidence."""

    __tablename__ = "awards"
    __table_args__ = (
        UniqueConstraint("source", "source_award_id"),
        CheckConstraint(
            "amount >= 0",
            name="amount_nonnegative",
        ),
        Index(
            "ix_awards_company_start_date",
            "company_id",
            "award_start_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_award_id: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    generated_internal_id: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    recipient_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    recipient_uei: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2),
        nullable=False,
    )
    awarding_agency: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    action_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    award_start_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    award_end_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
    )
    naics_code: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )
    psc_code: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )
    evidence_url: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped[Company] = relationship(back_populates="awards")
    signal: Mapped[Signal | None] = relationship(
        back_populates="award",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class Signal(TimestampMixin, Base):
    """Scored intelligence event derived from source evidence."""

    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint(
            "relevance_score BETWEEN 0 AND 100",
            name="relevance_score_range",
        ),
        Index(
            "ix_signals_startup_score",
            "is_startup_candidate",
            "relevance_score",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    award_id: Mapped[str] = mapped_column(
        ForeignKey("awards.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    signal_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="government_award",
        server_default="government_award",
    )
    relevance_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    matched_terms: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    reasons: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    organization_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    is_startup_candidate: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    quality_flags: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    company: Mapped[Company] = relationship(back_populates="signals")
    award: Mapped[Award] = relationship(back_populates="signal")


class IngestionRun(Base):
    """Durable audit record for one source-ingestion attempt."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            (
                "fetched_count >= 0 "
                "AND relevant_count >= 0 "
                "AND stored_count >= 0 "
                "AND duplicate_count >= 0"
            ),
            name="counts_nonnegative",
        ),
        Index(
            "ix_ingestion_runs_source_started_at",
            "source",
            "started_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="running",
        server_default="running",
    )
    requested_start_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    requested_end_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    fetched_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    relevant_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    stored_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    duplicate_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
