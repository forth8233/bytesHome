from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import ACCESS, TokenError, decode_token
from app.db.models import Member
from app.db.session import get_session
from app.services.config_service import ConfigService
from app.ha.adapter import HAAdapter

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Member:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        payload = decode_token(credentials.credentials, ACCESS)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    member = await session.get(Member, int(payload["sub"]))
    if member is None or not member.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account is disabled or missing")
    return member


async def require_admin(
    user: Annotated[Member, Depends(get_current_user)],
) -> Member:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator role required")
    return user


def get_config_service(request: Request) -> ConfigService:
    return request.app.state.config_service


def get_ha_adapter(request: Request) -> HAAdapter:
    return request.app.state.ha_adapter


CurrentUser = Annotated[Member, Depends(get_current_user)]
AdminUser = Annotated[Member, Depends(require_admin)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
ConfigDep = Annotated[ConfigService, Depends(get_config_service)]
HADep = Annotated[HAAdapter, Depends(get_ha_adapter)]
