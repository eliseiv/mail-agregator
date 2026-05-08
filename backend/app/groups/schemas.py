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
    name: str = Field(min_length=1, max_length=100)
    leader_user_id: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _trim_name(v)


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _trim_name(v)
