"""ADR-0022 §1.5 — revocation paths: logout / admin reset / cascade-delete.

round-43 (ADR-0022 §1.5): ``POST /logout`` was DECOUPLED from the Telegram
linkage. Logout now ends ONLY the web session — it no longer revokes
``telegram_links`` and no longer writes a ``telegram_link_revoked`` audit with
``reason="logout"``. The forced-revoke paths (admin password-reset,
cascade-delete) are UNCHANGED and still wipe all linkage.

This module asserts:

- logout KEEPS the link alive + writes NO ``telegram_link_revoked`` audit;
- admin reset still DROPS all target-user links (audit ``reason=password_reset``);
- cascade-delete additionally wipes ``telegram_notifications`` + ``users_settings``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.models import (
    AdminAudit,
    TelegramLink,
    TelegramNotification,
    User,
    UserSettings,
)

pytestmark = pytest.mark.integration


async def _login_admin_two_step(client: httpx.AsyncClient) -> str:
    """Log in as super-admin and return CSRF token (used for state-changing JSON calls)."""
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
    csrf = resp.cookies.get("mas_csrf")
    assert csrf, resp.text
    return csrf


class TestLogoutKeepsLink:
    """round-43 (ADR-0022 §1.5): logout is DECOUPLED from the Telegram link.

    The pre-round-43 assertion (logout DROPS the link + writes a
    ``telegram_link_revoked reason=logout`` audit) is INVERTED here: the link
    must survive logout and NO revoke audit may be written for the logout.
    """

    async def test_logout_keeps_link_and_writes_no_revoke_audit(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        tg_id = 70101
        await make_link(tg_id, admin.id)

        # Login then logout.
        csrf = await _login_admin_two_step(client)
        resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code in (302, 303), resp.text
        assert resp.headers.get("location") == "/login", resp.headers

        # Session cookies cleared (mas_session / mas_csrf / mas_login emptied).
        set_cookies = resp.headers.get_list("set-cookie")
        for name in ("mas_session", "mas_csrf", "mas_login"):
            cleared = [c for c in set_cookies if c.startswith(f"{name}=")]
            assert cleared, f"{name} must be cleared on logout; got {set_cookies}"
            # A cleared cookie carries an empty value (``name=;``) / Max-Age=0.
            assert any(
                c.startswith(f"{name}=;") or "Max-Age=0" in c or "max-age=0" in c for c in cleared
            ), f"{name} not actually cleared: {cleared}"

        async with factory() as ses:
            # round-43: the link row SURVIVES logout.
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id)
                )
            ).scalar_one_or_none()
            assert link is not None, "round-43: logout must NOT delete telegram_links"
            assert link.user_id == admin.id
            assert link.dead_at is None, "link must stay live (push must keep working)"

            # round-43: NO telegram_link_revoked audit is written by logout.
            revokes = (
                await ses.execute(
                    select(func.count())
                    .select_from(AdminAudit)
                    .where(AdminAudit.action == "telegram_link_revoked")
                )
            ).scalar_one()
            assert revokes == 0, "round-43: logout must NOT write telegram_link_revoked"

    async def test_logout_without_link_succeeds(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """Logout with no Telegram link at all → session revoked, no errors,
        302 → /login, and (trivially) no revoke audit."""
        csrf = await _login_admin_two_step(client)
        resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code in (302, 303), resp.text
        assert resp.headers.get("location") == "/login"

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            revokes = (
                await ses.execute(
                    select(func.count())
                    .select_from(AdminAudit)
                    .where(AdminAudit.action == "telegram_link_revoked")
                )
            ).scalar_one()
            assert revokes == 0

    async def test_session_invalid_after_logout(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        make_link: Any,
    ) -> None:
        """After logout the web session is revoked: a session-protected
        endpoint (``GET /api/telegram/links``) answers 401 with the stale
        cookies, even though the Telegram link itself survives."""
        s = get_settings()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            admin = (
                await ses.execute(select(User).where(User.username == s.ADMIN_LOGIN))
            ).scalar_one()
        tg_id = 70102
        await make_link(tg_id, admin.id)

        await _login_admin_two_step(client)
        # Sanity: the protected endpoint works WHILE logged in.
        ok = await client.get("/api/telegram/links")
        assert ok.status_code == 200, ok.text

        resp = await client.post(
            "/logout",
            headers={"X-CSRF-Token": client.cookies.get("mas_csrf") or ""},
        )
        assert resp.status_code in (302, 303), resp.text

        # The client jar now has the cleared cookies → session is dead.
        after = await client.get("/api/telegram/links")
        assert after.status_code == 401, after.text

        # …but the link is still there (push decoupled from web session).
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id)
                )
            ).scalar_one_or_none()
            assert link is not None and link.dead_at is None


class TestAdminResetRevokes:
    async def test_password_reset_drops_target_user_link(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        _, leader = leader_and_group
        # Pre-link the leader.
        await make_link(70201, leader.id)

        csrf = await _login_admin_two_step(client)
        resp = await client.post(
            f"/api/admin/users/{leader.id}/reset",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            link = (
                await ses.execute(select(TelegramLink).where(TelegramLink.user_id == leader.id))
            ).scalar_one_or_none()
            assert link is None

            # Audit row with reason=password_reset.
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
            assert (revokes[0].details or {}).get("reason") == "password_reset"


class TestDeleteUserCascades:
    async def test_delete_user_cascades_links_notifications_settings(
        self,
        client: httpx.AsyncClient,
        db_engine: AsyncEngine,
        leader_and_group: tuple[Any, User],
        create_member: Any,
        create_mail_account: Any,
        create_message: Any,
        tag_message_for_user: Any,
        make_link: Any,
    ) -> None:
        group, leader = leader_and_group
        member = await create_member(group.id, "doomed_member")

        # Pre-populate every related row for the member.
        await make_link(70301, member.id)
        # users_settings row.
        from backend.app.repositories.user_settings import UserSettingsRepo

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await UserSettingsRepo(ses).upsert_tg_notifications_enabled(
                user_id=member.id, enabled=False
            )
        # telegram_notifications row needs a message_id; build a tiny chain.
        acc = await create_mail_account(member.id, "member@x.com")
        msg = await create_message(acc.id, uid=70301)
        # Add a tag + link so the dispatcher SQL would see this. The
        # notification row itself can be inserted via the repo directly.
        await tag_message_for_user(member.id, msg.id, "VIP")
        async with factory() as ses, ses.begin():
            from backend.app.repositories.telegram_notifications import (
                TelegramNotificationsRepo,
            )

            # ADR-0024 §6: per-chat key — reserve the member's chat (70301).
            await TelegramNotificationsRepo(ses).try_reserve(
                message_id=msg.id, user_id=member.id, telegram_user_id=70301
            )

        # Sanity: all the rows are there.
        async with factory() as ses:
            assert (
                await ses.execute(select(TelegramLink).where(TelegramLink.user_id == member.id))
            ).scalar_one_or_none() is not None
            assert (
                await ses.execute(select(UserSettings).where(UserSettings.user_id == member.id))
            ).scalar_one_or_none() is not None
            assert (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.user_id == member.id)
                )
            ).scalar_one_or_none() is not None

        # Delete the user via the admin endpoint.
        csrf = await _login_admin_two_step(client)
        resp = await client.delete(
            f"/api/admin/users/{member.id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200, resp.text

        # All related rows are gone.
        async with factory() as ses:
            assert (
                await ses.execute(select(User).where(User.id == member.id))
            ).scalar_one_or_none() is None
            assert (
                await ses.execute(select(TelegramLink).where(TelegramLink.user_id == member.id))
            ).scalar_one_or_none() is None
            assert (
                await ses.execute(select(UserSettings).where(UserSettings.user_id == member.id))
            ).scalar_one_or_none() is None
            assert (
                await ses.execute(
                    select(TelegramNotification).where(TelegramNotification.user_id == member.id)
                )
            ).scalar_one_or_none() is None
