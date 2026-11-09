from datetime import timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession
from app.core.security import (
    REFRESH,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.db.models import Member, RefreshToken, utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 10


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class MemberOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    notify_service: str | None
    enabled: bool

    @classmethod
    def from_orm_member(cls, member: Member) -> "MemberOut":
        return cls(
            id=member.id,
            username=member.username,
            display_name=member.display_name,
            role=member.role,
            notify_service=member.notify_service,
            enabled=member.enabled,
        )


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    access_expires_at: str
    refresh_expires_at: str
    user: MemberOut


async def _issue_tokens(session: AsyncSession, member: Member) -> TokenOut:
    access, _, access_exp = create_access_token(member.id)
    refresh, jti, refresh_exp = create_refresh_token(member.id)
    session.add(RefreshToken(member_id=member.id, jti=jti, expires_at=refresh_exp.replace(tzinfo=None)))
    await session.commit()
    return TokenOut(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=access_exp.isoformat(),
        refresh_expires_at=refresh_exp.isoformat(),
        user=MemberOut.from_orm_member(member),
    )


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, session: DbSession) -> TokenOut:
    member = await session.scalar(select(Member).where(Member.username == payload.username))
    now = utcnow()
    if member is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if member.locked_until is not None and member.locked_until > now:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "account temporarily locked")
    if not member.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account is disabled")

    if not verify_password(payload.password, member.password_hash):
        member.failed_login_count += 1
        if member.failed_login_count >= MAX_FAILED_LOGINS:
            member.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            member.failed_login_count = 0
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    member.failed_login_count = 0
    member.locked_until = None
    member.last_login_at = now
    return await _issue_tokens(session, member)


@router.post("/refresh", response_model=TokenOut)
async def refresh(payload: RefreshRequest, session: DbSession) -> TokenOut:
    try:
        claims = decode_token(payload.refresh_token, REFRESH)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    row = await session.scalar(select(RefreshToken).where(RefreshToken.jti == claims["jti"]))
    if row is None or row.revoked or row.expires_at < utcnow():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh token is no longer valid")

    member = await session.get(Member, int(claims["sub"]))
    if member is None or not member.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account is disabled or missing")

    row.revoked = True
    await session.flush()
    return await _issue_tokens(session, member)


@router.post("/logout")
async def logout(
    user: CurrentUser,
    session: DbSession,
    payload: LogoutRequest | None = None,
) -> dict:
    query = select(RefreshToken).where(
        RefreshToken.member_id == user.id, RefreshToken.revoked.is_(False)
    )
    if payload and payload.refresh_token:
        try:
            claims = decode_token(payload.refresh_token, REFRESH)
            query = query.where(RefreshToken.jti == claims["jti"])
        except TokenError:
            return {"revoked": 0}
    rows = (await session.scalars(query)).all()
    for row in rows:
        row.revoked = True
    await session.commit()
    return {"revoked": len(rows)}


@router.get("/me", response_model=MemberOut)
async def me(user: CurrentUser) -> MemberOut:
    return MemberOut.from_orm_member(user)
