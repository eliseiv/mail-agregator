"""HTTP routes for messages: list, view, mark-read, attachment download.

JSON: ``/api/messages/...``.
HTML: ``/`` (inbox), ``/messages/{id}`` (view).
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse

from backend.app.deps import CurrentUser, DbSession
from backend.app.messages.schemas import (
    MarkReadRequest,
    MessageDetail,
    MessageListResponse,
)
from backend.app.messages.service import MessageService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.messages import MessagesRepo
from backend.app.templates import render

api = APIRouter(prefix="/api/messages", tags=["messages"])
html = APIRouter(tags=["messages-html"])


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


@api.get("", response_model=MessageListResponse)
async def list_messages(
    db: DbSession,
    user: CurrentUser,
    account_id: Annotated[int | None, Query(ge=1)] = None,
    unread: Annotated[bool | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageListResponse:
    return await MessageService(db).list_for_user(
        user_id=user.id,
        account_id=account_id,
        unread=unread,
        cursor=cursor,
        limit=limit,
    )


@api.get("/{message_id}", response_model=MessageDetail)
async def get_message(
    db: DbSession,
    user: CurrentUser,
    message_id: int = Path(..., ge=1),
) -> MessageDetail:
    return await MessageService(db).get(user_id=user.id, message_id=message_id)


@api.post(
    "/{message_id}/mark-read",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def mark_read(
    payload: MarkReadRequest,
    db: DbSession,
    user: CurrentUser,
    message_id: int = Path(..., ge=1),
) -> Response:
    async with db.begin():
        await MessageService(db).mark_read(user_id=user.id, message_id=message_id, payload=payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.get(
    "/{message_id}/attachments/{attachment_id}",
    response_class=StreamingResponse,
)
async def download_attachment(
    db: DbSession,
    user: CurrentUser,
    message_id: int = Path(..., ge=1),
    attachment_id: int = Path(..., ge=1),
) -> StreamingResponse:
    att, stream = await MessageService(db).stream_attachment(
        user_id=user.id,
        message_id=message_id,
        attachment_id=attachment_id,
    )
    # RFC 5987 filename* for non-ASCII; ASCII fallback for legacy clients.
    safe_ascii = att.filename.encode("ascii", "ignore").decode("ascii") or "file"
    encoded = quote(att.filename, safe="")
    headers = {
        "Content-Type": att.content_type or "application/octet-stream",
        "Content-Length": str(att.size_bytes),
        "Content-Disposition": (
            f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"
        ),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        stream,
        media_type=att.content_type or "application/octet-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@html.get("/", response_class=HTMLResponse)
async def inbox_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    account_id: Annotated[int | None, Query(ge=1)] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    unread: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    sess = request.state.session
    accounts = await MailAccountsRepo(db).list_for_user(user.id)
    listing = await MessageService(db).list_for_user(
        user_id=user.id,
        account_id=account_id,
        unread=unread,
        cursor=cursor,
        limit=limit,
    )
    unread_count = await MessagesRepo(db).count_unread_for_user(user.id)
    return await render(
        request,
        "inbox.html",
        {
            "items": listing.items,
            "next_cursor": listing.next_cursor,
            "accounts": accounts,
            "selected_account_id": account_id,
            "unread_only": bool(unread),
            "unread_count": unread_count,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/messages/{message_id}", response_class=HTMLResponse)
async def message_view_page(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    message_id: int = Path(..., ge=1),
) -> Response:
    sess = request.state.session
    detail = await MessageService(db).get(user_id=user.id, message_id=message_id)
    # Mark as read on first view (idempotent).
    async with db.begin():
        await MessageService(db).mark_read(
            user_id=user.id,
            message_id=message_id,
            payload=MarkReadRequest(is_read=True),
        )
    return await render(
        request,
        "message_view.html",
        {
            "message": detail,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


# Workaround for HTML pages that hit ``/`` when not authenticated:
# the FastAPI dependency raises ``NotAuthenticatedError`` -> 401. For the
# inbox we want a 302 redirect instead. We implement this by intercepting
# in main.create_app via an exception handler. See backend/app/main.py.

# Re-export
router = APIRouter()
router.include_router(api)
router.include_router(html)
