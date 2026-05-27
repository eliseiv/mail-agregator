"""ADR-0024 §3 (Sprint A) — link decision table + soft limit (items E & F).

Drives :class:`TelegramSSOService` directly against live Postgres (no mocks)
to validate the §3 decision table without going through HMAC/initData:

F. link logic:
   - NEW TG to one's own user → ``telegram_link_created`` (replaced=False);
   - REPEAT one's own TG → ``telegram_link_created`` (replaced=True, refresh);
   - someone else's TG via session-add → ``TelegramLinkOwnedByOtherError``
     (router → 409 ``tg_link_owned_by_other``);
   - someone else's TG via login-flow → rebind, ``telegram_link_rebound``.

E. soft limit ``TG_MAX_LINKS_PER_USER``:
   - ``count_active < limit`` → link created;
   - at the limit, session-add raises ``TelegramLinkLimitError`` + audits
     ``telegram_link_limit_reached``;
   - at the limit, login-flow is a NO-OP (does not raise) + audits
     ``telegram_link_limit_reached``.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.exceptions import (
    TelegramLinkLimitError,
    TelegramLinkOwnedByOtherError,
)
from backend.app.repositories.telegram_links import TelegramLinksRepo
from backend.app.telegram.sso_service import TelegramSSOService
from shared.config import get_settings
from shared.models import AdminAudit, TelegramLink, User

pytestmark = pytest.mark.integration

_IP = "203.0.113.7"
_UA = "qa-suite/1.0"


async def _audits(db_engine: AsyncEngine, action: str) -> list[AdminAudit]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return list(
            (await ses.execute(select(AdminAudit).where(AdminAudit.action == action)))
            .scalars()
            .all()
        )


async def _count_active(db_engine: AsyncEngine, user_id: int) -> int:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return await TelegramLinksRepo(ses).count_active_by_user_id(user_id)


# ---------------------------------------------------------------------------
# F. Link decision table
# ---------------------------------------------------------------------------


class TestLinkDecisionTable:
    async def test_new_own_tg_session_add_creates_link(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
    ) -> None:
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramSSOService(ses).link_session_add(
                telegram_user_id=410001, user_id=super_admin_user.id, ip=_IP, user_agent=_UA
            )

        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == 410001)
                )
            ).scalar_one()
            assert link.user_id == super_admin_user.id
            assert link.dead_at is None

        created = await _audits(db_engine, "telegram_link_created")
        assert len(created) == 1
        assert (created[0].details or {}).get("replaced") is False
        assert (created[0].details or {}).get("via") == "session_add"

    async def test_repeat_own_tg_is_refresh_replaced_true(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
    ) -> None:
        await make_link(410101, super_admin_user.id)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramSSOService(ses).link_session_add(
                telegram_user_id=410101, user_id=super_admin_user.id, ip=_IP, user_agent=_UA
            )

        created = await _audits(db_engine, "telegram_link_created")
        assert len(created) == 1
        assert (created[0].details or {}).get("replaced") is True
        # Still exactly one link for the user (refresh, not a new row).
        assert await _count_active(db_engine, super_admin_user.id) == 1

    async def test_other_users_tg_via_session_add_is_refused(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        """TG already owned by another user → session-add must NOT steal it."""
        _, leader = leader_and_group
        owned_tg = 410201
        await make_link(owned_tg, leader.id)  # belongs to the leader

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        with pytest.raises(TelegramLinkOwnedByOtherError):
            async with factory() as ses, ses.begin():
                await TelegramSSOService(ses).link_session_add(
                    telegram_user_id=owned_tg,
                    user_id=super_admin_user.id,
                    ip=_IP,
                    user_agent=_UA,
                )

        # Ownership unchanged.
        async with factory() as ses:
            link = (
                await ses.execute(
                    select(TelegramLink).where(TelegramLink.telegram_user_id == owned_tg)
                )
            ).scalar_one()
            assert link.user_id == leader.id

    async def test_other_users_tg_via_login_flow_rebinds(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        leader_and_group: tuple[Any, User],
        make_link: Any,
    ) -> None:
        """The password-proven login-flow MAY rebind another user's TG →
        ``telegram_link_rebound``."""
        _, leader = leader_and_group
        tg = 410301
        await make_link(tg, leader.id)

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            await TelegramSSOService(ses).link_pending(
                telegram_user_id=tg, user_id=super_admin_user.id, ip=_IP, user_agent=_UA
            )

        async with factory() as ses:
            link = (
                await ses.execute(select(TelegramLink).where(TelegramLink.telegram_user_id == tg))
            ).scalar_one()
            assert link.user_id == super_admin_user.id  # rebound

        rebound = await _audits(db_engine, "telegram_link_rebound")
        assert len(rebound) == 1
        assert (rebound[0].details or {}).get("previous_user_id") == leader.id


# ---------------------------------------------------------------------------
# E. Soft limit TG_MAX_LINKS_PER_USER
# ---------------------------------------------------------------------------


class TestSoftLimit:
    async def _seed_links(self, make_link: Any, user_id: int, count: int, base: int) -> None:
        for i in range(count):
            await make_link(base + i, user_id)

    async def test_under_limit_session_add_ok(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setenv("TG_MAX_LINKS_PER_USER", "3")
        get_settings.cache_clear()
        assert get_settings().TG_MAX_LINKS_PER_USER == 3

        await self._seed_links(make_link, super_admin_user.id, 2, base=420000)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            # 3rd link → under the cap of 3 (count was 2).
            await TelegramSSOService(ses).link_session_add(
                telegram_user_id=420099, user_id=super_admin_user.id, ip=_IP, user_agent=_UA
            )
        assert await _count_active(db_engine, super_admin_user.id) == 3
        get_settings.cache_clear()

    async def test_at_limit_session_add_raises_and_creates_no_link(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        monkeypatch: Any,
    ) -> None:
        """At the cap, session-add raises ``TelegramLinkLimitError`` and creates
        NO link.

        NB: the ``telegram_link_limit_reached`` audit is written inside the
        caller's transaction; when the caller wraps the call in
        ``async with ses.begin()`` (as the router does), the raise rolls the
        transaction back, so the audit is intentionally NOT asserted here — its
        persistence on the *non-raising* login-flow path is covered by
        :meth:`test_at_limit_login_flow_is_noop_not_raise`."""
        monkeypatch.setenv("TG_MAX_LINKS_PER_USER", "3")
        get_settings.cache_clear()

        await self._seed_links(make_link, super_admin_user.id, 3, base=420100)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        with pytest.raises(TelegramLinkLimitError):
            async with factory() as ses, ses.begin():
                await TelegramSSOService(ses).link_session_add(
                    telegram_user_id=420199,
                    user_id=super_admin_user.id,
                    ip=_IP,
                    user_agent=_UA,
                )

        # No new link created.
        assert await _count_active(db_engine, super_admin_user.id) == 3
        async with factory() as ses:
            n = int(
                (
                    await ses.execute(
                        select(func.count())
                        .select_from(TelegramLink)
                        .where(TelegramLink.telegram_user_id == 420199)
                    )
                ).scalar_one()
            )
            assert n == 0
        get_settings.cache_clear()

    async def test_at_limit_login_flow_is_noop_not_raise(
        self,
        db_engine: AsyncEngine,
        client: Any,
        super_admin_user: User,
        make_link: Any,
        monkeypatch: Any,
    ) -> None:
        """Login-flow at the cap must NOT raise (the login itself succeeds) —
        it audits ``telegram_link_limit_reached`` and silently no-ops."""
        monkeypatch.setenv("TG_MAX_LINKS_PER_USER", "3")
        get_settings.cache_clear()

        await self._seed_links(make_link, super_admin_user.id, 3, base=420200)
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        # No exception expected.
        async with factory() as ses, ses.begin():
            await TelegramSSOService(ses).link_pending(
                telegram_user_id=420299, user_id=super_admin_user.id, ip=_IP, user_agent=_UA
            )

        assert await _count_active(db_engine, super_admin_user.id) == 3
        limit_audits = await _audits(db_engine, "telegram_link_limit_reached")
        assert len(limit_audits) == 1
        assert (limit_audits[0].details or {}).get("via") == "login_flow"
        get_settings.cache_clear()
