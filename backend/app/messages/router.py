"""HTTP routes for messages: list, view, mark-read, attachment download.

JSON: ``/api/messages/...``.
HTML: ``/`` (inbox), ``/messages/{id}`` (view).
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import HTMLResponse, StreamingResponse

from backend.app.accounts.service import MailAccountService, _to_dto as _acc_to_dto
from backend.app.deps import CurrentScope, DbSession
from backend.app.groups.service import GroupsService
from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from backend.app.messages.schemas import (
    MarkReadRequest,
    MessageDetail,
    MessageListResponse,
)
from backend.app.messages.service import MessageService
from backend.app.tags.service import TagsService
from backend.app.templates import render

api = APIRouter(prefix="/api/messages", tags=["messages"])
html = APIRouter(tags=["messages-html"])


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


@api.get("", response_model=MessageListResponse)
async def list_messages(
    db: DbSession,
    scope: CurrentScope,
    account_id: Annotated[int | None, Query(ge=1)] = None,
    group_id: Annotated[int | None, Query(ge=1)] = None,
    tag_id: Annotated[int | None, Query(ge=1)] = None,
    unread: Annotated[bool | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MessageListResponse:
    return await MessageService(db).list_for_scope(
        scope,
        account_id=account_id,
        tag_id=tag_id,
        unread=unread,
        cursor=cursor,
        limit=limit,
        group_id=group_id,
    )


@api.get("/{message_id}", response_model=MessageDetail)
async def get_message(
    db: DbSession,
    scope: CurrentScope,
    message_id: int = Path(..., ge=1),
) -> MessageDetail:
    return await MessageService(db).get(scope=scope, message_id=message_id)


@api.post(
    "/{message_id}/mark-read",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def mark_read(
    payload: MarkReadRequest,
    db: DbSession,
    scope: CurrentScope,
    message_id: int = Path(..., ge=1),
) -> Response:
    async with db.begin():
        await MessageService(db).mark_read(scope=scope, message_id=message_id, payload=payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.get(
    "/{message_id}/attachments/{attachment_id}",
    response_class=StreamingResponse,
)
async def download_attachment(
    db: DbSession,
    scope: CurrentScope,
    message_id: int = Path(..., ge=1),
    attachment_id: int = Path(..., ge=1),
) -> StreamingResponse:
    att, stream = await MessageService(db).stream_attachment(
        scope=scope,
        message_id=message_id,
        attachment_id=attachment_id,
    )
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
    scope: CurrentScope,
    account_id: Annotated[str | None, Query()] = None,
    tag_id: Annotated[str | None, Query()] = None,
    group_id: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    unread: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    sess = request.state.session
    parsed_account_id: int | None = None
    if account_id and account_id.isdigit():
        parsed_account_id = int(account_id)
    parsed_tag_id: int | None = None
    if tag_id and tag_id.isdigit():
        parsed_tag_id = int(tag_id)
    parsed_group_id: int | None = None
    if group_id and group_id.isdigit():
        parsed_group_id = int(group_id)
    parsed_unread: bool | None = None
    if unread:
        parsed_unread = unread.lower() in ("1", "true", "on", "yes")

    # FE-FIX round-7 #2: super_admin gets a third filter — by group. We
    # always include the dropdown options when the caller is super_admin;
    # the template hides the <select> when ``groups`` is empty.
    groups: list = []
    if scope.is_super_admin:
        groups_resp = await GroupsService(db).list_for_scope(
            scope, q=None, page=1, limit=200
        )
        groups = groups_resp.items

    # Account / tag dropdowns cascade off the selected group (FE-FIX round-8):
    # when super_admin picks group N, "Все почты" narrows to mail accounts of
    # that group's users, and "Все теги" narrows to tags owned by the group's
    # members. The caller's own tags are merged in so the admin's personal
    # tags stay reachable. For non-super_admin, ``parsed_group_id`` is None
    # and the original behaviour is preserved.
    effective_group_id: int | None = (
        parsed_group_id if (scope.is_super_admin and parsed_group_id) else None
    )
    if effective_group_id is not None:
        member_ids = await UsersRepo(db).list_user_ids_in_group(effective_group_id)
        if member_ids:
            accs_map = await MailAccountsRepo(db).list_for_users(member_ids)
            users_map = await UsersRepo(db).get_many_by_ids(member_ids)
            accounts = [
                _acc_to_dto(a, users_map[a.user_id])
                for uid in member_ids
                for a in accs_map.get(uid, [])
                if a.user_id in users_map
            ]
            # Tags: members' tags ∪ caller's own tags. Dedupe by id.
            tags_by_id: dict[int, object] = {}
            for uid in member_ids + [scope.user_id]:
                for t in await TagsService(db).list_for_user(uid):
                    tags_by_id[t.id] = t
            tags = sorted(tags_by_id.values(), key=lambda t: (not t.is_builtin, t.name))
        else:
            accounts = []
            tags = await TagsService(db).list_for_user(scope.user_id)
    else:
        accounts = await MailAccountService(db).list_for_scope(scope)
        tags = await TagsService(db).list_for_user(scope.user_id)

    listing = await MessageService(db).list_for_scope(
        scope,
        account_id=parsed_account_id,
        tag_id=parsed_tag_id,
        group_id=effective_group_id,
        unread=parsed_unread,
        cursor=cursor,
        limit=limit,
    )
    unread_count = await MessageService(db).count_unread_for_scope(scope)
    return await render(
        request,
        "inbox.html",
        {
            "items": listing.items,
            "next_cursor": listing.next_cursor,
            "accounts": accounts,
            "tags": tags,
            "groups": groups,
            "selected_account_id": parsed_account_id,
            "selected_tag_id": parsed_tag_id,
            "selected_group_id": parsed_group_id if scope.is_super_admin else None,
            "unread_only": bool(parsed_unread),
            "unread_count": unread_count,
            "scope": scope,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


@html.get("/messages/{message_id}", response_class=HTMLResponse)
async def message_view_page(
    request: Request,
    db: DbSession,
    scope: CurrentScope,
    message_id: int = Path(..., ge=1),
) -> Response:
    sess = request.state.session
    detail = await MessageService(db).get(scope=scope, message_id=message_id)
    # Close the autobegun read-tx so the explicit begin() below does not collide.
    await db.commit()
    async with db.begin():
        await MessageService(db).mark_read(
            scope=scope,
            message_id=message_id,
            payload=MarkReadRequest(is_read=True),
        )
    return await render(
        request,
        "message_view.html",
        {
            "message": detail,
            "scope": scope,
            "csrf_token": sess.csrf_token,
            "session": sess,
        },
    )


# Re-export
router = APIRouter()
router.include_router(api)
router.include_router(html)
