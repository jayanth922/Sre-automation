import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from backend.rate_limit import rate_limit
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, auth, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


def _build_access_token(user: models.User) -> str:
    return auth.create_access_token(
        data={
            "sub": user.email,
            "role": user.role,
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "full_name": user.full_name or "",
        },
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        key=auth.REFRESH_COOKIE_NAME,
        value=raw,
        httponly=True,
        secure=auth.COOKIE_SECURE,
        samesite="lax",
        path="/",
        max_age=auth.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(auth.REFRESH_COOKIE_NAME, path="/")


async def _issue_refresh(db: AsyncSession, response: Response, user: models.User, family_id: uuid.UUID) -> None:
    raw = auth.generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=auth.REFRESH_TOKEN_EXPIRE_DAYS)
    await crud.create_refresh_session(db, user.id, auth.hash_refresh_token(raw), family_id, expires_at)
    _set_refresh_cookie(response, raw)

@router.post("/register", response_model=schemas.UserResponse, dependencies=[Depends(rate_limit(3, 60))])
async def register(user: schemas.UserCreate, db: AsyncSession = Depends(database.get_db)):
    db_user = await crud.get_user_by_email(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create new user and org
    return await crud.create_user(db=db, user=user)


@router.post("/token", response_model=schemas.Token, dependencies=[Depends(rate_limit(5, 60))])
async def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    response: Response,
    db: AsyncSession = Depends(database.get_db),
):
    user = await crud.get_user_by_email(db, email=form_data.username)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Start a fresh refresh-token family and set the httpOnly cookie.
    await _issue_refresh(db, response, user, family_id=uuid.uuid4())
    return {"access_token": _build_access_token(user), "token_type": "bearer"}


@router.post("/refresh", response_model=schemas.Token, dependencies=[Depends(rate_limit(30, 60))])
async def refresh_access_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(database.get_db),
):
    """Rotate the refresh token and mint a new access token. Detects reuse of a
    rotated token and revokes the whole family."""
    raw = request.cookies.get(auth.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh session")

    session = await crud.get_refresh_session_by_hash(db, auth.hash_refresh_token(raw))
    if not session:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid refresh session")

    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Reuse detection: a revoked token presented again → compromise the family.
    if session.revoked:
        await crud.revoke_refresh_family(db, session.family_id)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh session reuse detected")

    if expires_at <= now:
        await crud.revoke_refresh_session(db, session)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh session expired")

    user = await db.get(models.User, session.user_id)
    if not user or not user.is_active:
        await crud.revoke_refresh_family(db, session.family_id)
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="User inactive")

    # Rotate: revoke the presented token, issue a new one in the same family.
    await crud.revoke_refresh_session(db, session)
    await _issue_refresh(db, response, user, family_id=session.family_id)
    return {"access_token": _build_access_token(user), "token_type": "bearer"}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(database.get_db)):
    """Revoke the refresh family and clear the cookie."""
    raw = request.cookies.get(auth.REFRESH_COOKIE_NAME)
    if raw:
        session = await crud.get_refresh_session_by_hash(db, auth.hash_refresh_token(raw))
        if session:
            await crud.revoke_refresh_family(db, session.family_id)
    _clear_refresh_cookie(response)
    return {"status": "logged_out"}


@router.get("/me", response_model=schemas.UserProfileResponse)
async def read_current_user(
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    organization = await crud.get_org_by_id(db, user.org_id)
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")

    display_name = user.full_name.strip() if user.full_name and user.full_name.strip() else user.email
    return schemas.UserProfileResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        display_name=display_name,
        role=user.role,
        org_id=user.org_id,
        organization_name=organization.name,
        is_active=user.is_active,
        created_at=user.created_at,
    )


@router.post("/password")
async def reset_password(
    payload: schemas.PasswordResetRequest,
    user: models.User = Depends(get_current_user_and_org),
    db: AsyncSession = Depends(database.get_db),
):
    if not auth.verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.hashed_password = auth.get_password_hash(payload.new_password)
    await db.commit()
    return {"message": "Password updated successfully"}
