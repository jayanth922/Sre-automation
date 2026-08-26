import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError
from passlib.context import CryptContext

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    import secrets
    SECRET_KEY = secrets.token_urlsafe(64)
    import logging
    logging.getLogger(__name__).warning(
        "SECRET_KEY not set — generated ephemeral key. "
        "Set SECRET_KEY env var for stable tokens across restarts."
    )

ALGORITHM = "HS256"
# Short-lived access token (held in memory client-side); refresh handles longevity.
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
# Secure cookie flag — set true when served over TLS (production). Default false
# so local http (port-forward) still stores the cookie.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
REFRESH_COOKIE_NAME = "sentinel_refresh"

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ── Refresh tokens (opaque, rotated, hashed at rest) ─────────────────────────
import hashlib
import secrets as _secrets


def generate_refresh_token() -> str:
    """A high-entropy opaque token. Only its hash is stored server-side."""
    return _secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
