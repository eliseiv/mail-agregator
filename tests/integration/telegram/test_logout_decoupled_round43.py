"""round-43 (ADR-0022 §1.5) — logout DECOUPLED from the Telegram linkage.

Source of truth: ADR-0022 §1.5 "round-43" + qa_test_matrix (lines 1712-1714).

Background (prod bug): ``POST /logout`` used to call
``revoke_for_user(reason="logout")`` → ``delete_all_by_user_id`` which wiped
ALL ``telegram_links`` of the user. A *phantom* logout (stale tab / Telegram
WebApp reactivation submitting the "Выйти" form with a still-cookie-valid
session) repeatedly broke the link, producing a ``create → logout → create``
loop that round-38 self-heal could never close. round-43 removes that call:
logout now ends ONLY the web session; the link survives; push is self-sufficient
and does NOT depend on an active web session.

This module covers the matrix rows that the inverted/updated tests in
``test_logout_revoke.py`` / ``test_multi_tg_endpoints.py`` do not:

- case 4 — push AFTER logout: the recipient SQL still finds the live link
  (``dead_at IS NULL``) and the dispatcher delivers (mocked Bot API), proving
  push is independent of the web session;
- case 5 — self-heal is a NO-OP after logout (link still live, ``created_at``
  unchanged): the ``create → logout → create`` loop is gone;
- case 3 — explicit unlink (``DELETE /api/telegram/links/{tg}``) is the SOLE
  user revoke path: one link dropped (audit ``reason=user_unlink``), siblings
  live; foreign / missing TG → ``{"deleted": false}`` with no audit;
- case 6 — ``link_user_missing`` forced-revoke path is NOT broken.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.repositories.telegram_notifications import TelegramNotificationsRepo
from backend.app.telegram.notify_service import TelegramNotifyService
from shared.config import get_settings
from shared.models import AdminAudit, TelegramLink, TelegramNotification, User
from tests.integration.telegram.conftest import FakeSendResult, make_init_data

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    await client.post(
        "/login",
        data={"username": s.ADMIN_LOGIN},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = await client.post(
        "/login/password",
        data={"password": s.ADMIN_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    csrf = resp.cookies.get("mas_csrf") or client.cookies.get("mas_csrf")
    assert csrf, resp.text
    return csrf


async def _admin(db_engine: AsyncEngine) -> User:
    s = get_settings()
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return (await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))).scalar_one()


async def _get_link(db_engine: AsyncEngine, tg_id: int) -> TelegramLink | None:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return (
            await ses.execute(select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id))
        ).scalar_one_or_none()


def _payload_for(message_id: int) -> str:
    return json.dumps({"v": 1, "message_id": int(message_id), "source": "sync"})


# ---------------------------------------------------------------------------
# Case 4 — push AFTER logout (decoupled from the web session).
# ---------------------------------------------------------------------------


class TestPushAfterLogout:
    async def test_recipient_sql_still_finds_link_after_logout(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
    ) -> None:
        """After logout, ``list_recipients_for_message`` STILL returns the
        user's chat (link is live: ``dead_at IS NULL``) — push does not depend
        on the web session."""
        admin = await _admin(db_engine)
        tg_id = 580001
        # Link FIRST so the message's internal_date >= tl.created_at (the
        # first-link backfill predicate, ADR-0022 §2.2).
        await make_link(tg_id, admin.id)
        acc = await create_mail_account(admin.id, "pushlogout@example.com")
        msg = await create_message(acc.id, uid=580001)
        await tag_message_for_user(admin.id, msg.id, "VIP")

        # Log in then log out — the link must survive.
        csrf = await _login_admin(client)
        resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code in (302, 303), resp.text

        link = await _get_link(db_engine, tg_id)
        assert link is not None and link.dead_at is None

        # Recipient SQL (the production push entry-point) still resolves the user.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            recipients = await TelegramNotificationsRepo(ses).list_recipients_for_message(
                message_id=msg.id
            )
        tg_ids = {r.telegram_user_id for r in recipients}
        assert tg_id in tg_ids, "round-43: push recipient SQL must find the live link post-logout"

    async def test_dispatch_delivers_after_logout(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        fake_send_notification: Any,
    ) -> None:
        """End-to-end (mocked Bot API): a tagged message dispatched AFTER the
        user logged out is still delivered to the surviving chat, and a sent
        ``telegram_notifications`` row is written."""
        admin = await _admin(db_engine)
        tg_id = 580101
        await make_link(tg_id, admin.id)
        acc = await create_mail_account(admin.id, "dispatchlogout@example.com")
        msg = await create_message(acc.id, uid=580101)
        await tag_message_for_user(admin.id, msg.id, "VIP")
        fake_send_notification.push(FakeSendResult(kind="ok", telegram_message_id=99))

        csrf = await _login_admin(client)
        resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code in (302, 303), resp.text

        # Dispatch in its own session/tx (mirrors the worker entry-point).
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as s, s.begin():
            await TelegramNotifyService(s).dispatch_one_payload(_payload_for(msg.id))

        # The mocked Bot API was contacted for the surviving chat.
        chat_ids = [c["chat_id"] for c in fake_send_notification.calls]
        assert tg_id in chat_ids, "message must be delivered post-logout (push decoupled)"

        async with factory() as ses:
            row = (
                await ses.execute(
                    select(TelegramNotification).where(
                        TelegramNotification.message_id == msg.id,
                        TelegramNotification.telegram_user_id == tg_id,
                    )
                )
            ).scalar_one_or_none()
            assert row is not None and row.sent_at is not None


# ---------------------------------------------------------------------------
# Case 5 — self-heal is a NO-OP after logout (loop create→logout→create gone).
# ---------------------------------------------------------------------------


class TestSelfHealNoOpAfterLogout:
    async def test_relogin_self_heal_is_noop_link_unchanged(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """create → logout → (re-login) self-heal: because round-43 logout
        no longer drops the link, the subsequent self-heal sees a LIVE link of
        the same user → full NO-OP: ``created_at`` is NOT advanced and NO
        ``telegram_link_created`` audit is written. This is the elimination of
        the ``create → logout → create`` loop."""
        admin = await _admin(db_engine)
        tg_id = 590001

        # 1) create the link (out-of-band, like a prior WebApp open).
        await make_link(tg_id, admin.id)
        before = await _get_link(db_engine, tg_id)
        assert before is not None and before.dead_at is None
        created_at_0 = before.created_at

        # 2) login then logout — link must survive (round-43).
        csrf = await _login_admin(client)
        out = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert out.status_code in (302, 303), out.text
        after_logout = await _get_link(db_engine, tg_id)
        assert after_logout is not None, "round-43: logout must not drop the link"
        assert after_logout.created_at == created_at_0

        # 3) re-login and re-open the WebApp (self-heal). Link is LIVE & same
        #    user → NO-OP.
        await _login_admin(client)
        raw = make_init_data(telegram_user_id=tg_id)
        heal = await client.post("/api/telegram/auth", json={"init_data": raw})
        assert heal.status_code == 200, heal.text
        assert heal.json() == {"linked": False, "healed": True}

        final = await _get_link(db_engine, tg_id)
        assert final is not None
        assert final.user_id == admin.id
        assert final.dead_at is None
        assert final.created_at == created_at_0, (
            "self-heal after round-43 logout must be a NO-OP — created_at moved, "
            "which would re-open the push window and indicates the create→logout→create loop"
        )

        # NO-OP self-heal writes no create/rebound audit.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            n_created = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(AdminAudit)
                        .where(AdminAudit.action == "telegram_link_created")
                    )
                ).scalar_one()
            )
            assert n_created == 0


# ---------------------------------------------------------------------------
# Case 3 — explicit unlink is the SOLE user revoke path (round-43).
# ---------------------------------------------------------------------------


class TestExplicitUnlinkRound43:
    async def test_unlink_own_tg_drops_one_keeps_siblings_audit_user_unlink(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        admin = await _admin(db_engine)
        await make_link(595001, admin.id)
        await make_link(595002, admin.id)
        csrf = await _login_admin(client)

        resp = await client.delete("/api/telegram/links/595001", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": True}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            remaining = (
                (await ses.execute(select(TelegramLink).where(TelegramLink.user_id == admin.id)))
                .scalars()
                .all()
            )
            assert {link.telegram_user_id for link in remaining} == {595002}

            revokes = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_revoked")
                    )
                )
                .scalars()
                .all()
            )
            assert len(revokes) == 1
            details = revokes[0].details or {}
            assert details.get("reason") == "user_unlink"
            assert details.get("telegram_user_id") == 595001

    async def test_unlink_foreign_tg_returns_deleted_false_no_audit(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        _, leader = leader_and_group
        await make_link(595101, leader.id)
        csrf = await _login_admin(client)

        resp = await client.delete("/api/telegram/links/595101", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": False}

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == 595101)
                )
            ).scalar_one_or_none()
            assert link is not None and link.user_id == leader.id

            n_revokes = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(AdminAudit)
                        .where(AdminAudit.action == "telegram_link_revoked")
                    )
                ).scalar_one()
            )
            assert n_revokes == 0, "idempotent no-op unlink must not audit"

    async def test_unlink_nonexistent_tg_returns_deleted_false(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        csrf = await _login_admin(client)
        resp = await client.delete("/api/telegram/links/599999", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        assert resp.json() == {"deleted": False}


# ---------------------------------------------------------------------------
# Case 6 — link_user_missing forced-revoke path is NOT broken by round-43.
# ---------------------------------------------------------------------------


class TestLinkUserMissingStillRevokes:
    """The ``revoke_for_user`` forced-revoke method that the router invokes for
    ``reason="link_user_missing"`` (``POST /api/telegram/auth`` against a
    dangling link) is UNCHANGED by round-43 — only the *logout* call site was
    removed. The DB-level FK (``telegram_links.user_id ON DELETE CASCADE``)
    makes a genuinely orphaned row impossible to construct, so the router's
    defensive branch is exercised here at the service layer with the canonical
    ``reason`` the router passes."""

    async def test_revoke_for_user_link_user_missing_drops_links_and_audits(
        self,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        from backend.app.telegram.sso_service import TelegramSSOService

        _, leader = leader_and_group
        await make_link(596001, leader.id)
        await make_link(596002, leader.id)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramSSOService(ses).revoke_for_user(
                user_id=leader.id,
                reason="link_user_missing",
                ip="127.0.0.1",
                user_agent="qa",
            )

        async with factory() as ses:
            n_links = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(TelegramLink)
                        .where(TelegramLink.user_id == leader.id)
                    )
                ).scalar_one()
            )
            assert n_links == 0, "link_user_missing must drop all links of the user"

            revokes = (
                (
                    await ses.execute(
                        select(AdminAudit).where(AdminAudit.action == "telegram_link_revoked")
                    )
                )
                .scalars()
                .all()
            )
            assert len(revokes) == 1
            details = revokes[0].details or {}
            assert details.get("reason") == "link_user_missing"
            assert sorted(details.get("telegram_user_ids") or []) == [596001, 596002]
