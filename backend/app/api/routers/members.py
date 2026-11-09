from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.api.deps import AdminUser, DbSession
from app.api.routers.auth import MemberOut
from app.core.security import hash_password
from app.db.models import Member, MemberEntityPermission

router = APIRouter(prefix="/api/members", tags=["members"])


class MemberCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=64)
    role: str = Field(default="member", pattern="^(admin|member)$")
    notify_service: str | None = Field(default=None, max_length=128)


class MemberUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: str | None = Field(default=None, pattern="^(admin|member)$")
    notify_service: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


class PermissionIn(BaseModel):
    entity_pattern: str = Field(min_length=1, max_length=128)
    can_view: bool = True
    can_control: bool = False


class PermissionsUpdate(BaseModel):
    permissions: list[PermissionIn] = Field(default_factory=list)


@router.get("", response_model=list[MemberOut])
async def list_members(_: AdminUser, session: DbSession) -> list[MemberOut]:
    members = (await session.scalars(select(Member).order_by(Member.id))).all()
    return [MemberOut.from_orm_member(member) for member in members]


@router.post("", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def create_member(payload: MemberCreate, _: AdminUser, session: DbSession) -> MemberOut:
    existing = await session.scalar(select(Member).where(Member.username == payload.username))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")
    member = Member(
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        notify_service=payload.notify_service,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return MemberOut.from_orm_member(member)


@router.put("/{member_id}", response_model=MemberOut)
async def update_member(
    member_id: int, payload: MemberUpdate, _: AdminUser, session: DbSession
) -> MemberOut:
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if payload.role == "member" and member.role == "admin":
        await _ensure_not_last_admin(session, member_id)
    if payload.display_name is not None:
        member.display_name = payload.display_name
    if payload.password is not None:
        member.password_hash = hash_password(payload.password)
    if payload.role is not None:
        member.role = payload.role
    if payload.notify_service is not None:
        member.notify_service = payload.notify_service or None
    if payload.enabled is not None:
        member.enabled = payload.enabled
    await session.commit()
    await session.refresh(member)
    return MemberOut.from_orm_member(member)


@router.delete("/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(member_id: int, _: AdminUser, session: DbSession) -> None:
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    if member.role == "admin":
        await _ensure_not_last_admin(session, member_id)
    await session.delete(member)
    await session.commit()


@router.get("/{member_id}/permissions", response_model=list[PermissionIn])
async def get_permissions(member_id: int, _: AdminUser, session: DbSession) -> list[PermissionIn]:
    rows = (
        await session.scalars(
            select(MemberEntityPermission).where(MemberEntityPermission.member_id == member_id)
        )
    ).all()
    return [
        PermissionIn(
            entity_pattern=row.entity_pattern, can_view=row.can_view, can_control=row.can_control
        )
        for row in rows
    ]


@router.put("/{member_id}/permissions", response_model=list[PermissionIn])
async def replace_permissions(
    member_id: int, payload: PermissionsUpdate, _: AdminUser, session: DbSession
) -> list[PermissionIn]:
    member = await session.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    await session.execute(
        delete(MemberEntityPermission).where(MemberEntityPermission.member_id == member_id)
    )
    for item in payload.permissions:
        session.add(
            MemberEntityPermission(
                member_id=member_id,
                entity_pattern=item.entity_pattern,
                can_view=item.can_view,
                can_control=item.can_control,
            )
        )
    await session.commit()
    return payload.permissions


async def _ensure_not_last_admin(session, member_id: int) -> None:
    admin_count = await session.scalar(
        select(func.count()).select_from(Member).where(Member.role == "admin")
    )
    if admin_count and admin_count <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "cannot remove the last administrator")
