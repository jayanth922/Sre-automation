import os
import re
from typing import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from sre_agent.config import get_settings

# Typed settings: DEBUG=false must not enable SQLAlchemy echo (string truthiness
# previously treated any non-empty DEBUG value as True).
_settings = get_settings()

DATABASE_URL = _settings.database_url
SYNC_DATABASE_URL = _settings.sync_database_url

# Legacy module attributes retained for callers that still read them directly.
POSTGRES_USER = os.getenv("POSTGRES_USER", "sre_user")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "sre_password")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "sre_platform")


def _clean_port(value: str) -> str:
    """Kubernetes injects POSTGRES_PORT as 'tcp://<ip>:5432' whenever a Service is
    named 'postgres', which poisons a plain-port assumption. Recover the numeric
    port from either form."""
    m = re.search(r"(\d+)\s*$", value or "")
    return m.group(1) if m else "5432"


engine = create_async_engine(
    DATABASE_URL,
    echo=_settings.debug,
    future=True,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI Routes"""
    async with AsyncSessionLocal() as session:
        yield session


sync_engine = create_engine(
    SYNC_DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)
