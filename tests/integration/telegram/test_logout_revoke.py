"""ADR-0022 §1.5 — revocation paths: logout / admin reset / cascade-delete.

After each path the ``telegram_links`` row is gone, and (for logout / reset)
an audit row ``telegram_link_revoked`` with the right ``reason`` is written.
Cascade-delete additionally wipes ``telegram_notifications`` + ``users_settings``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import select
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


class TestLogoutRevokes:
    async def test_logout_drops_link_and_writes_audit(
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

        # Link row gone.
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == tg_id)
                )
            ).scalar_one_or_none()
            assert link is None

            # Audit row with reason=logout.
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
            assert (revokes[0].details or {}).get("reason") == "logout"
            # ADR-0024 §5: logout revokes ALL links and records them as a single
            # ``telegram_user_ids`` array (not the pre-ADR-0024 scalar field).
            assert (revokes[0].details or {}).get("telegram_user_ids") == [tg_id]


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
