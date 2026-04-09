from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.rate_limit import rate_limit
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from backend import schemas, crud, auth, database, models
from sre_agent.api.v1.auth_deps import get_current_user_and_org

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)

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
    db: AsyncSession = Depends(database.get_db)
):
    user = await crud.get_user_by_email(db, email=form_data.username)
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={
            "sub": user.email,
            "role": user.role,
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "full_name": user.full_name or "",
        },
        expires_delta=access_token_expires,
    )
    return {"access_token": access_token, "token_type": "bearer"}


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
