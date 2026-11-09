from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from jose import JWTError, jwt

from app.core.config import get_settings

_hasher = PasswordHasher()
ALGORITHM = "HS256"

ACCESS = "access"
REFRESH = "refresh"


class TokenError(Exception):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def _create_token(member_id: int, token_type: str, expires: timedelta) -> tuple[str, str, datetime]:
    settings = get_settings()
    jti = uuid4().hex
    expires_at = datetime.now(timezone.utc) + expires
    payload = {
        "sub": str(member_id),
        "type": token_type,
        "jti": jti,
        "iat": datetime.now(timezone.utc),
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)
    return token, jti, expires_at


def create_access_token(member_id: int) -> tuple[str, str, datetime]:
    settings = get_settings()
    return _create_token(member_id, ACCESS, timedelta(minutes=settings.access_token_minutes))


def create_refresh_token(member_id: int) -> tuple[str, str, datetime]:
    settings = get_settings()
    return _create_token(member_id, REFRESH, timedelta(days=settings.refresh_token_days))


def decode_token(token: str, expected_type: str) -> dict:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.app_secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise TokenError("invalid or expired token") from exc
    if payload.get("type") != expected_type:
        raise TokenError("unexpected token type")
    if not payload.get("sub") or not payload.get("jti"):
        raise TokenError("malformed token")
    return payload
