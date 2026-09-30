#!/usr/bin/env python3
"""A read-only inspect never carries a container filter the server can refuse."""

from types import SimpleNamespace

from sre_agent.executor import _live_args


def test_an_inspect_reads_the_whole_deployment():
    """2026-09-29: container `payment-service` in deployment
    `checkout-service` turned a harmless read into a REFUSED action."""
    action = SimpleNamespace(
        action_type="inspect",
        target="checkout-service",
        parameters={"namespace": "meridian", "container": "payment-service"},
    )
    assert _live_args(action) == {"name": "checkout-service", "namespace": "meridian"}
