"""Pydantic schemas for the auth module.

Two-step login (ADR-0016):

- :class:`LoginUsernameRequest` is submitted at step-1 (``POST /login``).
  It carries only the username — the server uses it to look up the user
  and decide where to send the browser next (set-password or password
  step).
- :class:`LoginPasswordRequest` is submitted at step-2
  (``POST /login/password``). The username is recovered from the
  ``mas_login`` cookie set by step-1, so it is *not* part of this schema —
  this prevents a client from submitting a different username at step-2
  than at step-1 (mitigates a confused-deputy attack).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class LoginUsernameRequest(BaseModel):
    """Step-1 of the two-step login flow (ADR-0016).

    The constraints mirror what ``UsersRepo`` accepts at user-creation time
    so callers can safely round-trip a value through this schema.
    """

    username: str = Field(min_length=1, max_length=64)

    @field_validator("username")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().lower()


class LoginPasswordRequest(BaseModel):
    """Step-2 of the two-step login flow (ADR-0016).

    Only the password is accepted from the form; the username comes from
    the ``mas_login`` cookie. ``password`` length is intentionally lax on
    input (``min_length=1``) — argon2 verify is the source of truth for
    correctness, and rejecting inputs by length here would only leak
    timing information.
    """

    password: str = Field(min_length=1, max_length=128)
    csrf_token: str | None = None  # form-only; JSON uses header


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    password_confirm: str = Field(min_length=8, max_length=128)
    csrf_token: str | None = None

    @field_validator("password")
    @classmethod
    def _password_complexity(cls, v: str) -> str:
        if not any(c.isalpha() for c in v):
            raise ValueError("password must contain at least one letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one digit")
        return v


class LoginJsonResponse(BaseModel):
    redirect: str
    kind: Literal["session_created", "set_password_required", "needs_password"]
