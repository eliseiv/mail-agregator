"""Pydantic schemas for mail forwarding (ADR-0034 §2).

- :class:`ForwardingUpsertRequest` — body of ``PUT /api/forwarding/me``.
- :class:`ForwardingDTO`           — response for ``GET`` / ``PUT`` (no secret;
  forwarding has none).

``forward_to`` is validated by the same **manual** e-mail pattern used in
``backend/app/accounts/schemas.py`` (exactly one ``@``, a domain with a dot,
no ``..``, length 3..254). ``EmailStr`` / pydantic-email is deliberately not
used in this project — uniformity wins. The field is optional at the Pydantic
layer (``str | None``) so a missing / empty ``forward_to`` surfaces as a
service-level ``validation_error`` with ``field=forward_to`` (per the API
contract, docs/04-api-contracts.md §4e) rather than a generic parse error.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

# Length bounds mirror ``accounts/schemas.py`` (3..254).
FORWARD_TO_MIN_LEN = 3
FORWARD_TO_MAX_LEN = 254


class ForwardToValidationError(ValueError):
    """Raised by :func:`validate_forward_to` on a malformed address.

    The service layer catches it and re-raises a domain
    ``ValidationError(field="forward_to")`` so the 400 response carries the
    documented field marker.
    """


def validate_forward_to(raw: str | None) -> str:
    """Validate + normalise a forward-to address (manual pattern).

    Mirrors ``accounts/schemas.py``: strips surrounding whitespace, enforces
    the 3..254 length window, requires exactly one ``@`` with a dotted domain
    and no ``..``. Returns the trimmed address on success; raises
    :class:`ForwardToValidationError` otherwise.
    """
    if raw is None:
        raise ForwardToValidationError("forward_to is required")
    email = raw.strip()
    if not (FORWARD_TO_MIN_LEN <= len(email) <= FORWARD_TO_MAX_LEN):
        raise ForwardToValidationError("forward_to length must be between 3 and 254")
    if "@" not in email or "." not in email.split("@", 1)[1]:
        raise ForwardToValidationError("forward_to is not a valid address")
    local, _, domain = email.partition("@")
    if not local or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ForwardToValidationError("forward_to is not a valid address")
    return email


class ForwardingUpsertRequest(BaseModel):
    """``PUT /api/forwarding/me {forward_to: str, is_active?: bool}``.

    Both fields are validated in the service (:class:`ForwardingService`):
    ``forward_to`` via :func:`validate_forward_to` and ``is_active`` defaults
    to ``true`` on create / "leave unchanged" on update.
    """

    forward_to: str | None = None
    is_active: bool | None = None


class ForwardingDTO(BaseModel):
    """Response shape for ``GET`` / ``PUT`` — there is no secret to hide."""

    id: int
    group_id: int
    forward_to: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
