"""Short-lived signed tickets for authenticating WebSocket connections.

See ``sre_agent/ws_auth.py`` for why this exists and how the tickets are
validated server-side.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from backend import auth, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org
from sre_agent.ws_auth import WS_TICKET_PURPOSE

WS_TICKET_TTL_SECONDS = 45

router = APIRouter(
    prefix="/ws-tickets",
    tags=["ws-tickets"],
    dependencies=[Depends(get_current_user_and_org)],
)


class WsTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post("", response_model=WsTicketResponse)
async def issue_ws_ticket(
    response: Response,
    user: models.User = Depends(get_current_user_and_org),
):
    """Mint a ticket scoped to the caller's org, valid just long enough for
    one WebSocket handshake (and to cover a reconnect attempt)."""
    response.headers["Cache-Control"] = "no-store"
    ticket = auth.create_access_token(
        data={
            "sub": user.email,
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "purpose": WS_TICKET_PURPOSE,
        },
        expires_delta=timedelta(seconds=WS_TICKET_TTL_SECONDS),
    )
    return WsTicketResponse(ticket=ticket, expires_in=WS_TICKET_TTL_SECONDS)
