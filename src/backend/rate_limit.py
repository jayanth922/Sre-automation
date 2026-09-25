"""
In-memory sliding-window rate limiter exposed as a FastAPI dependency.

Usage:
    from backend.rate_limit import rate_limit

    @router.post("/login", dependencies=[Depends(rate_limit(5, 60))])
    async def login(...): ...
"""

import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


# Shared store: maps a key (e.g. "ip:endpoint") to a list of timestamps.
_request_log: dict[str, list[float]] = defaultdict(list)


def _cleanup(timestamps: list[float], window_seconds: int, now: float) -> list[float]:
    """Return only the timestamps that fall inside the current window."""
    cutoff = now - window_seconds
    return [t for t in timestamps if t > cutoff]


def rate_limit(max_requests: int, window_seconds: int) -> Callable:
    """
    Dependency factory that returns a FastAPI-compatible dependency.

    Parameters
    ----------
    max_requests : int
        Maximum number of requests allowed inside the sliding window.
    window_seconds : int
        Size of the sliding window in seconds.
    """

    async def _limiter(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        # Include the route path so limits are per-endpoint.
        key = f"{client_ip}:{request.url.path}"
        now = time.time()

        # Prune expired entries for this key.
        _request_log[key] = _cleanup(_request_log[key], window_seconds, now)

        if len(_request_log[key]) >= max_requests:
            # Earliest request still in window determines when the client can retry.
            retry_after = int(window_seconds - (now - _request_log[key][0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

        _request_log[key].append(now)

    return _limiter
