"""The first-run claim, and the moment an install stops being unclaimed.

A fresh deployment ships with no configuration file and no accounts, so the
first visitor creates the founding organisation and its admin. Two things then
have to hold forever after: the claim route must refuse a second claim, and
self-serve registration must stay closed, because joining an existing
organisation is an invitation flow and nothing else.

These run against a real Postgres because the guard against two simultaneous
claims is a Postgres advisory lock. Under a SQLite stand-in every test here
would pass while the thing under test did nothing at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import auth as backend_auth
from backend import crud, database, models, rate_limit
from backend.routers import auth as auth_router
from sre_agent.api.v1 import setup as setup_router

_DATABASE_URL = os.getenv("DATABASE_URL", "")
_PASSWORD = "a-long-enough-password"

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL.startswith("postgresql"),
    reason="needs a live Postgres: concurrent claims are serialised by an advisory lock",
)


def _server_url() -> str:
    return _DATABASE_URL.rsplit("/", 1)[0]


def _admin_engine():
    return create_async_engine(f"{_server_url()}/postgres", isolation_level="AUTOCOMMIT")


_reachable: bool | None = None


def _postgres_reachable() -> bool:
    """Probed once: a developer without the compose stack up should skip, not error."""
    global _reachable
    if _reachable is None:

        async def probe() -> bool:
            engine = _admin_engine()
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return True
            except Exception:
                return False
            finally:
                await engine.dispose()

        _reachable = asyncio.run(probe())
    return _reachable


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    """Registration closed by default, and no test inherits another's rate quota."""
    if not _postgres_reachable():
        pytest.skip(f"no Postgres at {_server_url()}")
    monkeypatch.delenv("ALLOW_OPEN_REGISTRATION", raising=False)
    rate_limit._request_log.clear()
    yield
    rate_limit._request_log.clear()


@contextlib.asynccontextmanager
async def _scratch_database():
    """A throwaway database, so "no users exist" is a fact rather than a hope.

    Never the configured one: CI has already run migrations against that, and
    an empty users table is the entire premise of every test here.
    """
    name = f"sentinel_claim_{uuid.uuid4().hex[:12]}"

    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine = create_async_engine(f"{_server_url()}/{name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = _admin_engine()
        try:
            async with admin.connect() as conn:
                # A connection surviving dispose() would make the drop fail and
                # leave the scratch database behind on a long-lived host.
                with contextlib.suppress(Exception):
                    await conn.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname = :name AND pid <> pg_backend_pid()"
                        ),
                        {"name": name},
                    )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        finally:
            await admin.dispose()


@contextlib.asynccontextmanager
async def _client(session_factory):
    """Only the two routers under test, so the full agent runtime stays out."""

    async def scratch_db():
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.include_router(setup_router.router, prefix="/api/v1")
    app.include_router(auth_router.router)
    app.dependency_overrides[database.get_db] = scratch_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://claim-test"
    ) as client:
        yield client


def _body(email: str, org: str) -> dict:
    return {"email": email, "password": _PASSWORD, "full_name": "The Founder", "org_name": org}


def test_an_unclaimed_install_says_so():
    async def scenario():
        async with _scratch_database() as sessions, _client(sessions) as client:
            response = await client.get("/api/v1/setup/status")
            assert response.status_code == 200, response.text
            return response.json()

    body = asyncio.run(scenario())
    assert body["needs_setup"] is True
    assert body["open_registration"] is False


def test_claiming_creates_a_founding_admin_and_signs_them_in():
    async def scenario():
        async with _scratch_database() as sessions, _client(sessions) as client:
            response = await client.post(
                "/api/v1/setup/claim", json=_body("founder@example.com", "Platform Eng")
            )
            assert response.status_code == 201, response.text

            # The claim has to produce a real session, not just a row. An admin
            # sent back to /login would be signing in with a password they had
            # invented seconds earlier, for no reason.
            assert "sentinel_refresh" in client.cookies

            async with sessions() as session:
                assert await crud.count_users(session) == 1
                user = await crud.get_user_by_email(session, "founder@example.com")
                assert user is not None
                assert user.role == "admin"
                assert user.org_id is not None

            return response.json()["access_token"]

    token = asyncio.run(scenario())
    claims = jwt.decode(token, backend_auth.SECRET_KEY, algorithms=[backend_auth.ALGORITHM])
    assert claims["role"] == "admin"
    assert claims["org_id"]


def test_the_route_closes_behind_the_first_admin():
    async def scenario():
        async with _scratch_database() as sessions, _client(sessions) as client:
            first = await client.post(
                "/api/v1/setup/claim", json=_body("founder@example.com", "Platform Eng")
            )
            assert first.status_code == 201, first.text

            assert (await client.get("/api/v1/setup/status")).json()["needs_setup"] is False

            second = await client.post(
                "/api/v1/setup/claim", json=_body("squatter@example.com", "Squatter Inc")
            )
            assert second.status_code == 409, second.text

            async with sessions() as session:
                assert await crud.count_users(session) == 1

    asyncio.run(scenario())


def test_self_serve_registration_is_closed_once_the_install_is_claimed():
    """Left open, anyone who can reach the dashboard mints a tenant on your box."""

    async def scenario():
        async with _scratch_database() as sessions, _client(sessions) as client:
            claimed = await client.post(
                "/api/v1/setup/claim", json=_body("founder@example.com", "Platform Eng")
            )
            assert claimed.status_code == 201, claimed.text

            response = await client.post(
                "/auth/register", json=_body("stranger@example.com", "Their Own Tenant")
            )
            assert response.status_code == 403, response.text

            async with sessions() as session:
                assert await crud.count_users(session) == 1

    asyncio.run(scenario())


def test_an_operator_can_deliberately_reopen_registration(monkeypatch):
    monkeypatch.setenv("ALLOW_OPEN_REGISTRATION", "true")

    async def scenario():
        async with _scratch_database() as sessions, _client(sessions) as client:
            claimed = await client.post(
                "/api/v1/setup/claim", json=_body("founder@example.com", "Platform Eng")
            )
            assert claimed.status_code == 201, claimed.text

            assert (await client.get("/api/v1/setup/status")).json()["open_registration"] is True

            response = await client.post(
                "/auth/register", json=_body("tenant2@example.com", "Second Tenant")
            )
            assert response.status_code == 200, response.text

            async with sessions() as session:
                assert await crud.count_users(session) == 2

    asyncio.run(scenario())


def test_concurrent_claims_are_serialised():
    """Two claims at once must not leave two admins who cannot see each other.

    This also catches a degradation none of the sequential tests can: if
    ``lock_for_claim`` fails to recognise the dialect it silently becomes a
    no-op, and every other test in this file would still pass.
    """

    async def scenario():
        async with _scratch_database() as sessions:
            async with sessions() as session:
                assert session.get_bind().dialect.name == "postgresql"

            async with _client(sessions) as client:
                first, second = await asyncio.gather(
                    client.post("/api/v1/setup/claim", json=_body("one@example.com", "Org One")),
                    client.post("/api/v1/setup/claim", json=_body("two@example.com", "Org Two")),
                )

            assert sorted([first.status_code, second.status_code]) == [201, 409]

            async with sessions() as session:
                assert await crud.count_users(session) == 1
                orgs = (await session.execute(select(models.Organization))).scalars().all()
                assert len(orgs) == 1

    asyncio.run(scenario())
