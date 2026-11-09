from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.agent.proposals import ProposalError
from app.api.deps import CurrentUser

router = APIRouter(prefix="/api/proposal", tags=["proposal"])


def _service(request: Request):
    service = getattr(request.app.state, "proposal_service", None)
    if service is None:
        raise HTTPException(503, "proposal service is not available")
    return service


def _translate(exc: ProposalError) -> HTTPException:
    return HTTPException(exc.status_code, exc.message)


@router.get("")
async def list_proposals(
    user: CurrentUser,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    risk_level: str | None = None,
) -> dict[str, Any]:
    return await _service(request).list(
        page=page, page_size=page_size, status=status, risk_level=risk_level
    )


@router.get("/{proposal_id}")
async def get_proposal(proposal_id: int, user: CurrentUser, request: Request) -> dict[str, Any]:
    try:
        return await _service(request).get(proposal_id)
    except ProposalError as exc:
        raise _translate(exc) from exc


@router.post("/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: int, user: CurrentUser, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).approve(proposal_id, user)
    except ProposalError as exc:
        raise _translate(exc) from exc


@router.post("/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int,
    user: CurrentUser,
    request: Request,
    reason: str | None = None,
) -> dict[str, Any]:
    try:
        return await _service(request).reject(proposal_id, user, reason)
    except ProposalError as exc:
        raise _translate(exc) from exc


@router.post("/{proposal_id}/cancel")
async def cancel_proposal(
    proposal_id: int, user: CurrentUser, request: Request
) -> dict[str, Any]:
    try:
        return await _service(request).cancel(proposal_id, user)
    except ProposalError as exc:
        raise _translate(exc) from exc
