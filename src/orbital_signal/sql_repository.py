"""Async SQLAlchemy implementation of signal persistence."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from orbital_signal.database import AsyncSessionFactory
from orbital_signal.db_models import (
    Award as AwardModel,
)
from orbital_signal.db_models import (
    Company as CompanyModel,
)
from orbital_signal.db_models import (
    CompanyAlias as CompanyAliasModel,
)
from orbital_signal.db_models import (
    IngestionRun as IngestionRunModel,
)
from orbital_signal.db_models import (
    Signal as SignalModel,
)
from orbital_signal.domain import (
    AwardRecord,
    CompanyIntelligenceProfile,
    CompanySignal,
    IngestionResult,
)
from orbital_signal.profiles import build_company_profiles
from orbital_signal.repository import SignalReclassificationInput


def _normalize_company_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime across database backends."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class SqlAlchemySignalRepository:
    """Persist complete company, award, and signal graphs."""

    def __init__(
        self,
        session_factory: AsyncSessionFactory,
    ) -> None:
        self._session_factory = session_factory

    async def upsert(
        self,
        award: AwardRecord,
        signal: CompanySignal,
    ) -> bool:
        async with self._session_factory.begin() as session:
            company = await self._upsert_company(
                session,
                award,
                signal,
            )
            await self._upsert_alias(session, company, award)
            award_model = await self._upsert_award(
                session,
                company,
                award,
                signal,
            )
            return await self._upsert_signal(
                session,
                company,
                award_model,
                signal,
            )

    async def list(
        self,
        *,
        minimum_score: int = 0,
        minimum_priority_score: int = 0,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        statement = (
            select(SignalModel, AwardModel, CompanyModel)
            .join(
                AwardModel,
                SignalModel.award_id == AwardModel.id,
            )
            .join(
                CompanyModel,
                SignalModel.company_id == CompanyModel.id,
            )
            .where(
                SignalModel.relevance_score >= minimum_score,
                SignalModel.priority_score >= minimum_priority_score,
            )
            .order_by(
                SignalModel.priority_score.desc(),
                SignalModel.relevance_score.desc(),
                AwardModel.amount.desc(),
            )
            .limit(limit)
        )

        if startup_candidates_only:
            statement = statement.where(SignalModel.is_startup_candidate.is_(True))

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        return [
            self._to_domain_signal(
                signal_model,
                award_model,
                company_model,
            )
            for signal_model, award_model, company_model in rows
        ]

    async def list_company_profiles(
        self,
        *,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanyIntelligenceProfile]:
        statement = (
            select(SignalModel, AwardModel, CompanyModel)
            .join(
                AwardModel,
                SignalModel.award_id == AwardModel.id,
            )
            .join(
                CompanyModel,
                SignalModel.company_id == CompanyModel.id,
            )
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        signals = [
            self._to_domain_signal(
                signal_model,
                award_model,
                company_model,
            )
            for signal_model, award_model, company_model in rows
        ]

        profiles = build_company_profiles(signals)

        if startup_candidates_only:
            profiles = [profile for profile in profiles if profile.startup_candidate]

        return profiles[:limit]

    async def list_reclassification_inputs(
        self,
    ) -> list[SignalReclassificationInput]:
        statement = (
            select(AwardModel, SignalModel, CompanyModel)
            .outerjoin(
                SignalModel,
                SignalModel.award_id == AwardModel.id,
            )
            .join(
                CompanyModel,
                AwardModel.company_id == CompanyModel.id,
            )
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        return [
            SignalReclassificationInput(
                award=self._to_domain_award(award_model),
                signal=(
                    self._to_domain_signal(
                        signal_model,
                        award_model,
                        company_model,
                    )
                    if signal_model is not None
                    else None
                ),
                detected_at=_ensure_utc(award_model.detected_at),
            )
            for award_model, signal_model, company_model in rows
        ]

    async def delete_signal(self, signal_id: str) -> bool:
        async with self._session_factory.begin() as session:
            signal = await session.get(
                SignalModel,
                signal_id,
            )

            if signal is None:
                return False

            await session.delete(signal)
            return True

    async def count(self) -> int:
        statement = select(func.count()).select_from(SignalModel)

        async with self._session_factory() as session:
            value = await session.scalar(statement)

        return int(value or 0)

    async def start_ingestion(
        self,
        *,
        source: str,
        start_date: date,
        end_date: date,
    ) -> str:
        run_id = str(uuid4())

        async with self._session_factory.begin() as session:
            session.add(
                IngestionRunModel(
                    id=run_id,
                    source=source,
                    status="running",
                    requested_start_date=start_date,
                    requested_end_date=end_date,
                )
            )

        return run_id

    async def finish_ingestion(
        self,
        run_id: str,
        result: IngestionResult,
    ) -> None:
        async with self._session_factory.begin() as session:
            ingestion_run = await session.get(
                IngestionRunModel,
                run_id,
            )
            if ingestion_run is None:
                raise LookupError(f"ingestion run not found: {run_id}")

            ingestion_run.status = "completed"
            ingestion_run.fetched_count = result.fetched_count
            ingestion_run.relevant_count = result.relevant_count
            ingestion_run.stored_count = result.stored_count
            ingestion_run.duplicate_count = result.duplicate_count
            ingestion_run.error_message = None
            ingestion_run.completed_at = datetime.now(UTC)

    async def fail_ingestion(
        self,
        run_id: str,
        error_message: str,
    ) -> None:
        async with self._session_factory.begin() as session:
            ingestion_run = await session.get(
                IngestionRunModel,
                run_id,
            )
            if ingestion_run is None:
                raise LookupError(f"ingestion run not found: {run_id}")

            ingestion_run.status = "failed"
            ingestion_run.error_message = error_message
            ingestion_run.completed_at = datetime.now(UTC)

    @staticmethod
    async def _upsert_company(
        session: AsyncSession,
        award: AwardRecord,
        signal: CompanySignal,
    ) -> CompanyModel:
        normalized_name = _normalize_company_name(award.recipient_name)
        recipient_uei = award.recipient_uei.strip().upper() if award.recipient_uei else None
        recipient_uei = recipient_uei or None
        company: CompanyModel | None = None

        if recipient_uei:
            company = await session.scalar(
                select(CompanyModel).where(func.upper(func.trim(CompanyModel.uei)) == recipient_uei)
            )

        if company is None:
            candidates = (
                await session.scalars(
                    select(CompanyModel)
                    .outerjoin(
                        CompanyAliasModel,
                        CompanyAliasModel.company_id == CompanyModel.id,
                    )
                    .where(
                        or_(
                            CompanyModel.normalized_name == normalized_name,
                            and_(
                                CompanyAliasModel.normalized_alias == normalized_name,
                                CompanyAliasModel.source == award.source,
                            ),
                        )
                    )
                    .distinct()
                )
            ).all()

            # Names can belong to several recipients. Only infer identity from
            # a single match, and never override a conflicting explicit UEI.
            if len(candidates) == 1:
                candidate = candidates[0]
                if not recipient_uei or candidate.uei is None:
                    company = candidate

            if company is None and not recipient_uei:
                unidentified = [candidate for candidate in candidates if candidate.uei is None]
                if len(unidentified) == 1:
                    company = unidentified[0]

        if company is None:
            identity = (
                f"uei:{recipient_uei}" if recipient_uei else f"name:{normalized_name}:{uuid4()}"
            )
            company = CompanyModel(
                id=_stable_id(identity),
                canonical_name=award.recipient_name,
                normalized_name=normalized_name,
                uei=recipient_uei,
                organization_type=(signal.organization_type.value),
            )
            session.add(company)
        else:
            if company.uei is None and recipient_uei is not None:
                company.uei = recipient_uei
            company.organization_type = signal.organization_type.value

        return company

    @staticmethod
    async def _upsert_alias(
        session: AsyncSession,
        company: CompanyModel,
        award: AwardRecord,
    ) -> None:
        normalized_alias = _normalize_company_name(award.recipient_name)
        alias_id = await session.scalar(
            select(CompanyAliasModel.id).where(
                CompanyAliasModel.company_id == company.id,
                CompanyAliasModel.normalized_alias == normalized_alias,
                CompanyAliasModel.source == award.source,
            )
        )

        if alias_id is None:
            session.add(
                CompanyAliasModel(
                    company_id=company.id,
                    alias=award.recipient_name,
                    normalized_alias=normalized_alias,
                    source=award.source,
                )
            )

    @staticmethod
    async def _upsert_award(
        session: AsyncSession,
        company: CompanyModel,
        award: AwardRecord,
        signal: CompanySignal,
    ) -> AwardModel:
        award_id = _stable_id(f"{award.source}:{award.source_award_id}")
        award_model = await session.get(AwardModel, award_id)
        if award_model is None and award.generated_internal_id:
            award_model = await session.scalar(
                select(AwardModel).where(
                    AwardModel.source == award.source,
                    AwardModel.generated_internal_id == award.generated_internal_id,
                )
            )

        values = {
            "company_id": company.id,
            "source": award.source,
            "source_award_id": award.source_award_id,
            "generated_internal_id": (award.generated_internal_id),
            "recipient_name": award.recipient_name,
            "recipient_uei": award.recipient_uei,
            "amount": Decimal(str(award.amount)),
            "awarding_agency": award.awarding_agency,
            "description": award.description,
            "action_date": award.action_date,
            "award_start_date": award.start_date,
            "award_end_date": award.end_date,
            "naics_code": award.naics_code,
            "psc_code": award.psc_code,
            "evidence_url": str(award.source_url),
        }

        if award_model is None:
            award_model = AwardModel(
                id=award_id,
                detected_at=signal.detected_at,
                **values,
            )
            session.add(award_model)
        else:
            for field, value in values.items():
                setattr(award_model, field, value)

        return award_model

    @staticmethod
    async def _upsert_signal(
        session: AsyncSession,
        company: CompanyModel,
        award: AwardModel,
        signal: CompanySignal,
    ) -> bool:
        signal_model = await session.get(
            SignalModel,
            signal.signal_id,
        )
        if signal_model is None:
            signal_model = await session.scalar(
                select(SignalModel).where(SignalModel.award_id == award.id)
            )
        inserted = signal_model is None

        values = {
            "company_id": company.id,
            "award_id": award.id,
            "signal_type": signal.signal_type,
            "relevance_score": signal.relevance_score,
            "matched_terms": list(signal.matched_terms),
            "reasons": list(signal.reasons),
            "priority_score": signal.priority_score,
            "priority_reasons": list(signal.priority_reasons),
            "organization_type": (signal.organization_type.value),
            "is_startup_candidate": (signal.is_startup_candidate),
            "quality_flags": list(signal.quality_flags),
        }

        if signal_model is None:
            session.add(
                SignalModel(
                    id=signal.signal_id,
                    detected_at=signal.detected_at,
                    **values,
                )
            )
        else:
            for field, value in values.items():
                setattr(signal_model, field, value)

        return inserted

    @staticmethod
    def _to_domain_award(
        award: AwardModel,
    ) -> AwardRecord:
        return AwardRecord(
            source=award.source,
            source_award_id=award.source_award_id,
            generated_internal_id=award.generated_internal_id,
            recipient_name=award.recipient_name,
            recipient_uei=award.recipient_uei,
            amount=float(award.amount),
            awarding_agency=award.awarding_agency,
            description=award.description,
            action_date=award.action_date,
            start_date=award.award_start_date,
            end_date=award.award_end_date,
            naics_code=award.naics_code,
            psc_code=award.psc_code,
            source_url=award.evidence_url,
        )

    @staticmethod
    def _to_domain_signal(
        signal: SignalModel,
        award: AwardModel,
        company: CompanyModel,
    ) -> CompanySignal:
        return CompanySignal(
            signal_id=signal.id,
            company_name=award.recipient_name,
            company_uei=award.recipient_uei or company.uei,
            signal_type=signal.signal_type,
            occurred_on=award.action_date or award.award_start_date,
            amount=float(award.amount),
            agency=award.awarding_agency,
            summary=award.description,
            relevance_score=signal.relevance_score,
            matched_terms=list(signal.matched_terms),
            reasons=list(signal.reasons),
            priority_score=signal.priority_score,
            priority_reasons=list(signal.priority_reasons),
            organization_type=signal.organization_type,
            is_startup_candidate=(signal.is_startup_candidate),
            quality_flags=list(signal.quality_flags),
            source=award.source,
            source_award_id=award.source_award_id,
            evidence_url=award.evidence_url,
            detected_at=_ensure_utc(signal.detected_at),
        )
