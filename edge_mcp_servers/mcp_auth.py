"""Bearer authentication at the MCP ASGI transport boundary."""

import os
import secrets
from typing import Any, Iterable, Tuple


def bearer_authorized(
    headers: Iterable[Tuple[bytes, bytes]],
    expected_token: str | None = None,
) -> bool:
    expected = expected_token or os.getenv("MCP_SERVICE_TOKEN", "").strip()
    if not expected:
        return False
    authorization = ""
    for name, value in headers:
        if name.lower() == b"authorization":
            authorization = value.decode("latin-1")
            break
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False
    return secrets.compare_digest(authorization[len(prefix) :], expected)


class MCPBearerAuthMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            from relay_credentials import capture_relay_credentials

            capture_relay_credentials(scope.get("headers", ()))
        if scope.get("type") == "http" and not bearer_authorized(
            scope.get("headers", ())
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Unauthorized MCP transport",
                }
            )
            return
        await self.app(scope, receive, send)


def run_authenticated_sse(mcp: Any, *, host: str, port: int) -> None:
    """Run FastMCP's SSE ASGI app behind fail-closed bearer middleware."""
    if not os.getenv("MCP_SERVICE_TOKEN", "").strip():
        raise RuntimeError("MCP_SERVICE_TOKEN is required")
    app_factory = getattr(mcp, "sse_app", None)
    if app_factory is None:
        raise RuntimeError("Installed MCP SDK does not expose FastMCP.sse_app()")

    import uvicorn

    uvicorn.run(MCPBearerAuthMiddleware(app_factory()), host=host, port=port)
