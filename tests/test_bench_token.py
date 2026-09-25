#!/usr/bin/env python3
"""Tests for the benchmark runner's access token.

These guard one specific failure, which happened: the runner logged in once
and reused that string for the whole campaign. Access tokens live 15 minutes
(`ACCESS_TOKEN_EXPIRE_MINUTES`) and a single incident is allowed 45
(`BENCH_INCIDENT_TIMEOUT_SEC`), so a Phase 0 smoke run died on a 401 while
polling for recovery — seventeen minutes in, with the fault already injected,
the agent still spending money, and nothing scored.

The bug is invisible on short runs and fatal on long ones, which is the wrong
way round for a benchmark whose whole purpose is long runs. Hence tests: the
renewal schedule has no observable effect until the moment it is needed.
"""

import asyncio
import base64
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1] / "evals" / "benchmarks"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "sre_bench_token_under_test", BENCHMARKS / "sre_bench.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _jwt(exp: datetime) -> str:
    """A JWT-shaped string carrying `exp`. Signature is irrelevant here —
    nothing in the runner verifies it, and the server is not involved."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp.timestamp())}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


class _Clock:
    """A controllable `now`, so a 15-minute expiry does not take 15 minutes."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self, tz=None) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _token_with(runner, monkeypatch, *, lifetimes, clock):
    """A `_Token` whose logins hand back tokens of the given lifetimes."""
    logins: list[datetime] = []
    remaining = list(lifetimes)

    async def _fake_login(client, creds):
        logins.append(clock.now)
        return _jwt(clock.now + remaining.pop(0))

    monkeypatch.setattr(runner, "_login", _fake_login)
    monkeypatch.setattr(runner, "datetime", _PatchedDatetime(clock))
    return runner._Token(client=None, creds=None), logins


class _PatchedDatetime:
    """`datetime` with a controllable `now`; everything else passes through."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def now(self, tz=None):
        return self._clock(tz)

    def __getattr__(self, name):
        return getattr(datetime, name)


# --- Reading the expiry -------------------------------------------------------


def test_the_lifetime_comes_from_the_tokens_own_exp_claim(runner):
    """Scheduling from the claim rather than a constant means a server
    configured with a different TTL does not reintroduce the bug."""
    issued = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)

    lifetime = runner._token_lifetime(_jwt(issued + timedelta(minutes=15)), issued)

    assert lifetime == timedelta(minutes=15)


@pytest.mark.parametrize(
    "token", ["not-a-jwt", "", "a.b.c", "header.!!!notbase64!!!.sig"]
)
def test_an_unreadable_token_yields_no_lifetime_rather_than_raising(runner, token):
    """An opaque token is a reason to fall back to a short schedule, not a
    reason to fail the campaign before it starts."""
    issued = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc)

    assert runner._token_lifetime(token, issued) is None


# --- The renewal schedule -----------------------------------------------------


def test_the_first_use_logs_in(runner, monkeypatch):
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, logins = _token_with(
        runner, monkeypatch, lifetimes=[timedelta(minutes=15)], clock=clock
    )

    asyncio.run(token.value())

    assert len(logins) == 1


def test_a_token_well_inside_its_life_is_reused(runner, monkeypatch):
    """Renewing per request would turn a 45-minute poll into hundreds of
    logins."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, logins = _token_with(
        runner, monkeypatch, lifetimes=[timedelta(minutes=15)], clock=clock
    )

    asyncio.run(token.value())
    clock.advance(timedelta(minutes=8))  # 8 of 15 → under the 0.6 fraction
    asyncio.run(token.value())

    assert len(logins) == 1


def test_a_token_past_the_renew_fraction_is_replaced_before_it_expires(
    runner, monkeypatch
):
    """The point of the fraction: renew at minute 9 of 15, not at minute 16."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, logins = _token_with(
        runner,
        monkeypatch,
        lifetimes=[timedelta(minutes=15), timedelta(minutes=15)],
        clock=clock,
    )

    first = asyncio.run(token.value())
    clock.advance(timedelta(minutes=10))  # 10 of 15 → past 0.6
    second = asyncio.run(token.value())

    assert len(logins) == 2
    assert second != first


def test_a_poll_longer_than_the_token_never_presents_an_expired_one(
    runner, monkeypatch
):
    """The regression itself, at the scale that produced it: a 45-minute
    incident timeout against a 15-minute token."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, logins = _token_with(
        runner, monkeypatch, lifetimes=[timedelta(minutes=15)] * 10, clock=clock
    )

    issued_at: dict[str, datetime] = {}
    for _ in range(45):  # one poll a minute for the full incident timeout
        value = asyncio.run(token.value())
        issued_at.setdefault(value, clock.now)
        age = clock.now - issued_at[value]
        assert age < timedelta(minutes=15), "presented a token past its expiry"
        clock.advance(timedelta(minutes=1))

    assert len(logins) > 1, "the token was never renewed across 45 minutes"


def test_an_opaque_token_falls_back_to_the_short_schedule(runner, monkeypatch):
    """With no readable `exp` the runner must still renew — on the
    conservative default rather than never."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    logins: list[datetime] = []

    async def _fake_login(client, creds):
        logins.append(clock.now)
        return "opaque-token"

    monkeypatch.setattr(runner, "_login", _fake_login)
    monkeypatch.setattr(runner, "datetime", _PatchedDatetime(clock))
    token = runner._Token(client=None, creds=None)

    asyncio.run(token.value())
    clock.advance(runner._TOKEN_FALLBACK_LIFETIME)
    asyncio.run(token.value())

    assert len(logins) == 2


# --- Failing closed -----------------------------------------------------------


def test_an_already_expired_token_raises_instead_of_looping(runner, monkeypatch):
    """A renewal schedule already in the past would make every request a
    login. Stopping is better than hammering /auth/token for 45 minutes."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, logins = _token_with(
        runner, monkeypatch, lifetimes=[timedelta(minutes=-2)], clock=clock
    )

    with pytest.raises(RuntimeError, match="already-expired"):
        asyncio.run(token.value())

    assert len(logins) == 1, "retried rather than failing closed"


# --- What the request layer actually sends ------------------------------------


def test_the_header_carries_the_current_token(runner, monkeypatch):
    """Call sites pass the token object where they used to pass a string, so
    the bearer value has to come from `headers()`, not from `repr`."""
    clock = _Clock(datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc))
    token, _ = _token_with(
        runner, monkeypatch, lifetimes=[timedelta(minutes=15)], clock=clock
    )

    headers = asyncio.run(token.headers())

    assert headers["Authorization"] == f"Bearer {asyncio.run(token.value())}"


def test_no_request_site_still_interpolates_the_token_object():
    """`f"Bearer {jwt}"` with a `_Token` in scope produces a repr and a 401
    on every request. It must not survive anywhere in the runner."""
    source = (BENCHMARKS / "sre_bench.py").read_text()

    assert 'f"Bearer {jwt}"' not in source
