"""Shared authentication dependencies for all API v1 routers."""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend import crud, models, database
from backend.auth import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")


async def get_current_user_and_org(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(database.get_db),
) -> models.User:
    """Validate JWT and return the authenticated user."""
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await crud.get_user_by_email(db, email=payload.get("sub"))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is deactivated")
    return user


async def require_admin(
    user: models.User = Depends(get_current_user_and_org),
) -> models.User:
    """Require the authenticated user to be an org admin."""
    if user.role != models.UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return user
