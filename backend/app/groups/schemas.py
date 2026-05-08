"""Pydantic schemas for the groups module (ADR-0019)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class UserBriefDTO(BaseModel):
    """Shrunk user summary for embedding (leader / owner / member)."""

    id: int
    username: str
    display_name: str | None = None
    role: str | None = None


class GroupDTO(BaseModel):
    id: int
    name: str
    leader: UserBriefDTO
    members_count: int
    created_at: datetime


class GroupDetailDTO(BaseModel):
    id: int
    name: str
    leader: UserBriefDTO
    members: list[UserBriefDTO]
    created_at: datetime


class GroupsListResponse(BaseModel):
    items: list[GroupDTO]
    total: int
    page: int
    limit: int


def _trim_name(v: str) -> str:
    v = v.strip()
    if not (1 <= len(v) <= 100):
        raise ValueError("name must be 1..100 characters")
    return v


class GroupCreateRequest(BaseModel):
    """Body of ``POST /api/admin/groups``.

    Backwards-compat note: the original contract accepted just ``name`` and
    ``leader_user_id``. The endpoint now also accepts an optional
    ``member_ids`` list — additional users to be promoted to
    ``group_member`` of the new group in the same transaction. See
    ADR-0019 §5 + the new "create group with members" UI flow.
    """

    name: str = Field(min_length=1, max_length=100)
    leader_user_id: int = Field(ge=1)
    member_ids: list[int] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _trim_name(v)

    @field_validator("member_ids")
    @classmethod
    def _v_member_ids(cls, v: list[int]) -> list[int]:
        # Reject duplicates and non-positive ids; preserve order.
        seen: set[int] = set()
        out: list[int] = []
        for item in v:
            if not isinstance(item, int) or item < 1:
                raise ValueError("member_ids must contain positive integers")
            if item in seen:
                raise ValueError("member_ids must not contain duplicates")
            seen.add(item)
            out.append(item)
        return out


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _trim_name(v)


class EligibleUserDTO(BaseModel):
    """Output row of ``GET /api/admin/users/eligible``.

    Lightweight view for the "create group" form: every user that the
    super-admin may legally pick as leader / member (i.e. not the
    super-admin themselves).
    """

    id: int
    username: str
    display_name: str | None = None
    role: str
    group: dict[str, str | int] | None = None  # {"id": int, "name": str} | None


class EligibleUsersResponse(BaseModel):
    items: list[EligibleUserDTO]
