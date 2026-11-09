from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.chat.service import ChatService

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _service(request: Request) -> ChatService:
    service = getattr(request.app.state, "chat_service", None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "chat service is not available")
    return service


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: int | None = None


class SessionIn(BaseModel):
    title: str = Field(default="新会话", max_length=60)


class ConfirmIn(BaseModel):
    confirm_token: str = Field(min_length=8, max_length=128)
    yaml: str | None = Field(default=None, max_length=8000)


@router.post("")
async def chat(payload: ChatIn, user: CurrentUser, request: Request) -> dict[str, Any]:
    return await _service(request).chat(user, payload.message, payload.session_id)


@router.post("/confirm")
async def confirm_automation(
    payload: ConfirmIn, user: CurrentUser, request: Request
) -> dict[str, Any]:
    return await _service(request).confirm_automation(
        user,
        payload.confirm_token,
        yaml_content=payload.yaml,
        ip=_client_ip(request),
    )


@router.get("/sessions")
async def list_sessions(user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    return await _service(request).list_sessions(user.id)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionIn, user: CurrentUser, request: Request
) -> dict[str, Any]:
    return await _service(request).create_session(user.id, payload.title)


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: int, user: CurrentUser, request: Request, limit: int = 100
) -> list[dict[str, Any]]:
    return await _service(request).list_messages(user.id, session_id, max(1, min(500, limit)))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: int, user: CurrentUser, request: Request) -> None:
    await _service(request).delete_session(user.id, session_id)
