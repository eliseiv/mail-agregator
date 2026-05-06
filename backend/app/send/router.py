"""HTTP routes for sending mail.

JSON: ``POST /api/messages/send``.
HTML: ``GET /compose`` (new + reply).

The send endpoint accepts both ``application/json`` and
``application/x-www-form-urlencoded`` (no-JS fallback, ADR-0015). On
form-encoded success the response is a ``303`` redirect to ``/`` with a
flash; on validation/SMTP error the compose form is re-rendered with the
submitted values preserved.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError

from backend.app.deps import CurrentUser, DbSession, is_form_request
from backend.app.exceptions import DomainError, ValidationError
from backend.app.flash import flash
from backend.app.rate_limit import LIMIT_MESSAGE_SEND, consume
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.send.schemas import SendMessageRequest, SendMessageResponse
from backend.app.send.service import SendService
from backend.app.templates import render

api = APIRouter(prefix="/api/messages", tags=["send"])
html = APIRouter(tags=["send-html"])

# Splitter for multi-value form fields (to / cc / bcc): a comma OR a
# semicolon, surrounded by optional whitespace. We do NOT split on plain
# whitespace — RFC 5322 local-parts may legally contain unusual characters
# but never commas / semicolons (when unquoted).
_ADDRESS_SPLIT_RE = re.compile(r"[,;]")


def _split_addresses(raw: str | None) -> list[str]:
    """Split a comma- or semicolon-separated address string into a clean list.

    Empty input returns an empty list. Whitespace around each entry is
    trimmed and empty entries are dropped before downstream RFC 5322
    validation runs in the Pydantic schema.
    """
    if not raw:
        return []
    parts = _ADDRESS_SPLIT_RE.split(raw)
    return [p.strip() for p in parts if p.strip()]


async def _parse_send_form(request: Request) -> SendMessageRequest:
    """Parse a form-encoded send request into the Pydantic schema."""
    form = await request.form()

    def _str(field: str) -> str:
        v = form.get(field)
        return v if isinstance(v, str) else ""

    from_acc_raw = _str("from_account_id").strip() or "0"
    in_reply_raw = _str("in_reply_to_message_id").strip()

    try:
        from_acc_id = int(from_acc_raw)
    except ValueError as exc:
        raise ValidationError(
            "from_account_id must be an integer", field="from_account_id"
        ) from exc

    in_reply_to: int | None
    if in_reply_raw:
        try:
            in_reply_to = int(in_reply_raw)
        except ValueError as exc:
            raise ValidationError(
                "in_reply_to_message_id must be an integer",
                field="in_reply_to_message_id",
            ) from exc
    else:
        in_reply_to = None

    try:
        return SendMessageRequest.model_validate(
            {
                "from_account_id": from_acc_id,
                "to": _split_addresses(_str("to") or None),
                "cc": _split_addresses(_str("cc") or None) or None,
                "bcc": _split_addresses(_str("bcc") or None) or None,
                "subject": _str("subject") or None,
                "body": _str("body"),
                "in_reply_to_message_id": in_reply_to,
            }
        )
    except PydanticValidationError as exc:
        raise ValidationError("Invalid form payload") from exc


async def _rerender_compose(
    request: Request,
    db: DbSession,
    *,
    user_id: int,
    error_message: str,
    status_code: int,
) -> Response:
    """Re-render the compose form preserving submitted values.

    Accepts ``user_id`` as a primitive (not the ORM ``User``) because this
    helper is invoked after a rolled-back ``async with db.begin():`` block —
    at that point the ORM instance's attributes are expired, and reading
    ``user.id`` here would trigger a sync lazy-load that crashes the asyncpg
    driver with ``MissingGreenlet`` (BUG-003). Callers must extract
    primitives from the ORM ``User`` *before* opening the write transaction.
    """
    sess = request.state.session
    accounts = await MailAccountsRepo(db).list_for_user(user_id)
    active_accounts = [a for a in accounts if a.is_active]

    form = await request.form()

    def _s(field: str) -> str:
        v = form.get(field)
        return v if isinstance(v, str) else ""

    from_id_str = _s("from_account_id").strip()
    try:
        default_from_id = int(from_id_str) if from_id_str else None
    except ValueError:
        default_from_id = None

    in_reply_str = _s("in_reply_to_message_id").strip()
    reply_to: int | None
    try:
        reply_to = int(in_reply_str) if in_reply_str else None
    except ValueError:
        reply_to = None

    return await render(
        request,
        "compose.html",
        {
            "accounts": active_accounts,
            "csrf_token": sess.csrf_token,
            "session": sess,
            "form": {
                "to": _s("to"),
                "cc": _s("cc"),
                "bcc": _s("bcc"),
                "subject": _s("subject"),
                "body": _s("body"),
            },
            "default_from_account_id": default_from_id,
            "reply_to": reply_to,
            "error_message": error_message,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Send (JSON or form)
# ---------------------------------------------------------------------------


@api.post(
    "/send",
    response_model=None,  # mixed JSON / redirect for forms
)
async def send_message(
    request: Request,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    """Send a message. Accepts JSON or form-encoded (ADR-0015)."""
    # Snapshot ORM-bound primitives before the write transaction. After a
    # rollback inside ``async with db.begin():`` the ``user`` instance's
    # attributes are expired; touching ``user.id`` in the ``except`` branch
    # would trigger a sync lazy-load and crash asyncpg with
    # ``MissingGreenlet`` (BUG-003).
    user_id = user.id

    await consume(LIMIT_MESSAGE_SEND, str(user_id))

    is_form = is_form_request(request)

    if is_form:
        payload = await _parse_send_form(request)
    else:
        body = await request.json()
        try:
            payload = SendMessageRequest.model_validate(body)
        except PydanticValidationError as exc:
            raise ValidationError("Invalid JSON payload") from exc

    try:
        async with db.begin():
            result = await SendService(db).send(user_id=user_id, payload=payload)
    except DomainError as exc:
        if is_form:
            await flash(request, "error", exc.message)
            return await _rerender_compose(
                request,
                db,
                user_id=user_id,
                error_message=exc.message,
                status_code=exc.status_code,
            )
        raise

    if is_form:
        await flash(request, "success", "Письмо отправлено")
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(content=SendMessageResponse.model_validate(result).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# HTML: compose
# ---------------------------------------------------------------------------


@html.get("/compose", response_class=HTMLResponse)
async def compose_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    reply_to: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    sess = request.state.session
    accounts = await MailAccountsRepo(db).list_for_user(user.id)
    active_accounts = [a for a in accounts if a.is_active]

    prefill: dict[str, object] = {
        "to": "",
        "cc": "",
        "bcc": "",
        "subject": "",
        "body": "",
    }
    default_from_id: int | None = active_accounts[0].id if active_accounts else None

    if reply_to is not None:
        original = await MessagesRepo(db).get_owned(message_id=reply_to, user_id=user.id)
        if original is not None:
            subject = (original.subject or "").strip()
            prefill["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            quoted = "\n".join(f"> {line}" for line in original.body_text.splitlines())
            from_display = (
                f"{original.from_name} <{original.from_addr}>"
                if original.from_name
                else original.from_addr
            )
            prefill["body"] = (
                f"\n\nOn {original.internal_date.isoformat()} {from_display} wrote:\n{quoted}"
            )
            prefill["to"] = original.from_addr
            default_from_id = original.mail_account_id

    return await render(
        request,
        "compose.html",
        {
            "accounts": active_accounts,
            "form": prefill,
            "default_from_account_id": default_from_id,
            "reply_to": reply_to,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


router = APIRouter()
router.include_router(api)
router.include_router(html)
