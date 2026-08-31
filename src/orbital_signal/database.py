"""Async SQLAlchemy engine, metadata, and session construction."""

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by all persistent models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


AsyncSessionFactory = async_sessionmaker[AsyncSession]


def build_async_engine(
    database_url: str,
    *,
    echo: bool = False,
) -> AsyncEngine:
    """Create an async engine without opening a connection immediately."""

    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
    )


def build_session_factory(
    engine: AsyncEngine,
) -> AsyncSessionFactory:
    """Create request-safe async sessions bound to an engine."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
