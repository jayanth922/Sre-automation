import os
import re
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

# Get DB URL from environment or use default
# Note: In Docker, hostname is 'postgres'. Locally, it might be 'localhost'
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


# Prefer an explicit DATABASE_URL (the deployment sets a correct one); only build
# it from parts as a fallback, tolerating the k8s tcp:// port form.
DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{_clean_port(POSTGRES_PORT)}/{POSTGRES_DB}"
)

# Create Async Engine
engine = create_async_engine(
    DATABASE_URL,
    echo=True if os.getenv("DEBUG") else False,
    future=True,
    pool_pre_ping=True,
    # Use NullPool regarding some async context scenarios with Celery/Background tasks if needed, 
    # but strictly for FastAPI standard pooling is fine.
)

# Configured Session Local
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession
)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for FastAPI Routes"""
    async with AsyncSessionLocal() as session:
        yield session

# ----------------------------------------------------------------------
# Synchronous DB Access (For Audit Logging / Celery)
# ----------------------------------------------------------------------
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Use standard postgresql driver (psycopg2) for sync
SYNC_DATABASE_URL = DATABASE_URL.replace("postgresql+asyncpg", "postgresql")

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    pool_pre_ping=True,
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)
