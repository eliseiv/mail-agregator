"""Backend-logic tests for mailbox team selection & transfer (ADR-0031).

These exercise ``MailAccountService.create`` / ``.update`` (transfer) and
``GroupsService.selectable_teams`` directly against a real Postgres, using the
function-scoped, rolled-back ``db_session`` fixture from the top-level
``tests/conftest.py``. Only the IMAP/SMTP test-login coroutines are stubbed so
the network is never touched — everything else is the production code path.

Lives in ``tests/unit`` so CI (which gates only tests/unit|worker|frontend)
actually runs it. The DB-backed cases ``skip`` cleanly when Postgres is not
reachable (see ``db_session``); CI provides the service container.

Source of truth:
- docs/adr/ADR-0031-mailbox-team-selection.md (§2 create, §3 transfer,
  §4 authz matrix, §5 GET /api/my/groups, §6 audit).
- docs/05-modules.md §9 (accounts), docs/04-api-contracts.md,
  docs/06-security.md.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.accounts.schemas import (
    MailAccountCreateRequest,
    MailAccountUpdateRequest,
)
from backend.app.accounts.service import MailAccountService
from backend.app.deps import VisibilityScope
from backend.app.exceptions import ForbiddenError, NotFoundError
from backend.app.groups.service import GroupsService
from shared.models import (
    Group,
    MailAccount,
    User,
    UserGroup,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# DB isolation. These cases ``commit`` (the service opens its own
# ``async with db.begin()`` write-tx, which requires no outer transaction to
# be open), so the top-level ``db_session`` rollback is not enough. We
# TRUNCATE the touched tables before each test for deterministic state.
# ---------------------------------------------------------------------------

_TRUNCATE_ORDER = ["mail_accounts", "user_groups", "admin_audit", "users", "groups"]


@pytest_asyncio.fixture(autouse=True)
async def _truncate(db_session: AsyncSession) -> Any:
    joined = ", ".join(_TRUNCATE_ORDER)
    await db_session.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
    await db_session.commit()
    yield


# ---------------------------------------------------------------------------
# Stub IMAP/SMTP test-login so create() never hits the network.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_test_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.accounts import service as svc_mod

    async def _ok(**_: Any) -> None:
        return None

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok)


# ---------------------------------------------------------------------------
# Seeder — users/groups/memberships/accounts inside the rolled-back session.
# ---------------------------------------------------------------------------


class Seeder:
    """Minimal domain-row builder. ``flush`` only — the session rolls back."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session
        self._n = 0

    async def super_admin(self) -> User:
        self._n += 1
        u = User(
            username=f"sa_{self._n}",
            role="super_admin",
            group_id=None,
            password_reset_required=False,
        )
        self.s.add(u)
        await self.s.flush()
        return u

    async def _membership(self, *, user_id: int, group_id: int) -> None:
        self.s.add(UserGroup(user_id=user_id, group_id=group_id))
        await self.s.flush()

    async def group_with_leader(self, name: str) -> tuple[Group, User]:
        self._n += 1
        leader = User(
            username=f"leader_{self._n}",
            role="group_leader",
            password_reset_required=False,
        )
        self.s.add(leader)
        await self.s.flush()
        g = Group(name=name, leader_user_id=leader.id)
        self.s.add(g)
        await self.s.flush()
        leader.group_id = g.id
        await self.s.flush()
        await self._membership(user_id=leader.id, group_id=g.id)
        return g, leader

    async def member(self, group_id: int) -> User:
        self._n += 1
        u = User(
            username=f"member_{self._n}",
            role="group_member",
            group_id=group_id,
            password_reset_required=False,
        )
        self.s.add(u)
        await self.s.flush()
        await self._membership(user_id=u.id, group_id=group_id)
        return u

    async def add_membership(self, *, user_id: int, group_id: int) -> None:
        """Add an *additional* (non-home) membership (ADR-0030)."""
        await self._membership(user_id=user_id, group_id=group_id)

    async def mail_account(
        self,
        *,
        user_id: int,
        group_id: int | None,
        email: str,
        auth_type: str = "password",
    ) -> MailAccount:
        new_id = int(
            (await self.s.execute(text("SELECT nextval('mail_accounts_id_seq')"))).scalar_one()
        )
        kwargs: dict[str, Any] = {
            "id": new_id,
            "user_id": user_id,
            "group_id": group_id,
            "email": email,
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_ssl": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_ssl": True,
            "smtp_starttls": False,
            "auth_type": auth_type,
        }
        if auth_type == "oauth_outlook":
            kwargs["encrypted_password"] = None
            kwargs["oauth_provider"] = "outlook"
            kwargs["oauth_refresh_token_encrypted"] = b"refresh-blob"
            kwargs["imap_host"] = "outlook.office365.com"
            kwargs["smtp_host"] = "smtp.office365.com"
            kwargs["smtp_ssl"] = False
            kwargs["smtp_starttls"] = True
        else:
            kwargs["encrypted_password"] = b"x"
        acc = MailAccount(**kwargs)
        self.s.add(acc)
        await self.s.flush()
        return acc

    async def audit_rows(self, action: str) -> list[dict[str, Any]]:
        rows = await self.s.execute(
            text(
                "SELECT actor_user_id, target_user_id, action, details "
                "FROM admin_audit WHERE action = :a ORDER BY id"
            ),
            {"a": action},
        )
        return [
            {
                "actor_user_id": int(r[0]),
                "target_user_id": (int(r[1]) if r[1] is not None else None),
                "action": r[2],
                "details": r[3],
            }
            for r in rows
        ]

    async def account_group_id(self, account_id: int) -> int | None:
        gid = (
            await self.s.execute(
                text("SELECT group_id FROM mail_accounts WHERE id = :i"),
                {"i": account_id},
            )
        ).scalar_one()
        return int(gid) if gid is not None else None


@pytest_asyncio.fixture
async def seed(db_session: AsyncSession) -> Seeder:
    return Seeder(db_session)


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _scope(user: User, group_ids: frozenset[int] | None = None) -> VisibilityScope:
    if user.role == "super_admin":
        gids: frozenset[int] = frozenset()
    elif group_ids is not None:
        gids = group_ids
    else:
        gids = frozenset({user.group_id}) if user.group_id is not None else frozenset()
    return VisibilityScope(
        user_id=user.id,
        role=user.role,  # type: ignore[arg-type]
        group_id=user.group_id,
        group_ids=gids,
    )


def _create_payload(**overrides: Any) -> MailAccountCreateRequest:
    base: dict[str, Any] = {
        "email": "box@example.com",
        "password": "secret-imap-pwd",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_starttls": False,
    }
    base.update(overrides)
    return MailAccountCreateRequest(**base)


# ===========================================================================
# CREATE — group_id selection (ADR-0031 §2/§4)
# ===========================================================================


class TestCreateGroupSelection:
    async def test_create_no_group_id_lands_in_owner_home_group(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Ordinary member, group_id omitted → box in the owner's home group."""
        g, _leader = await seed.group_with_leader("Alpha")
        member = await seed.member(g.id)
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(scope=_scope(member), payload=_create_payload())
        assert await seed.account_group_id(dto.id) == g.id

    async def test_create_super_admin_self_no_group_id_is_personal_null(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """super_admin creating on self with no group_id → group_id = NULL."""
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(scope=_scope(sa), payload=_create_payload())
        assert await seed.account_group_id(dto.id) is None

    async def test_member_create_with_valid_group_id_in_scope(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Multi-team member can file a new box into any of their teams."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        member = await seed.member(g1.id)
        await seed.add_membership(user_id=member.id, group_id=g2.id)
        await db_session.commit()

        scope = _scope(member, group_ids=frozenset({g1.id, g2.id}))
        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(scope=scope, payload=_create_payload(group_id=g2.id))
        assert await seed.account_group_id(dto.id) == g2.id

    async def test_member_create_with_group_id_outside_scope_forbidden(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Member cannot file a box into a team they don't belong to → 403."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        member = await seed.member(g1.id)  # only in g1
        await db_session.commit()

        scope = _scope(member, group_ids=frozenset({g1.id}))
        svc = MailAccountService(db_session)
        with pytest.raises(ForbiddenError) as ei:
            async with db_session.begin():
                await svc.create(scope=scope, payload=_create_payload(group_id=g2.id))
        assert ei.value.message == "user_not_in_group_scope"
        assert ei.value.status_code == 403

    async def test_member_create_with_nonexistent_group_id_404(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _l1 = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        await db_session.commit()

        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError) as ei:
            async with db_session.begin():
                await svc.create(scope=_scope(member), payload=_create_payload(group_id=999_999))
        assert ei.value.message == "group_not_found"
        assert ei.value.status_code == 404

    async def test_leader_create_self_into_own_membership(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Leader filing their own box → any of their memberships allowed."""
        g1, leader = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        await seed.add_membership(user_id=leader.id, group_id=g2.id)
        await db_session.commit()

        scope = _scope(leader, group_ids=frozenset({g1.id, g2.id}))
        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(scope=scope, payload=_create_payload(group_id=g2.id))
        assert await seed.account_group_id(dto.id) == g2.id

    async def test_leader_create_for_member_only_into_leader_home_group(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Leader filing a *member's* box → only the leader's home team."""
        g1, leader = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        await db_session.commit()

        scope = _scope(leader, group_ids=frozenset({g1.id}))
        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(
                scope=scope,
                payload=_create_payload(group_id=g1.id, target_user_id=member.id),
            )
        assert await seed.account_group_id(dto.id) == g1.id

    async def test_leader_create_for_member_into_other_team_forbidden(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Leader cannot file a member's box into a non-home team → 403."""
        g1, leader = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        member = await seed.member(g1.id)
        # Leader is ALSO a member of g2, but acting on a *member* pins target to home.
        await seed.add_membership(user_id=leader.id, group_id=g2.id)
        await db_session.commit()

        scope = _scope(leader, group_ids=frozenset({g1.id, g2.id}))
        svc = MailAccountService(db_session)
        with pytest.raises(ForbiddenError) as ei:
            async with db_session.begin():
                await svc.create(
                    scope=scope,
                    payload=_create_payload(group_id=g2.id, target_user_id=member.id),
                )
        assert ei.value.message == "user_not_in_group_scope"

    async def test_super_admin_create_into_any_existing_group(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _leader = await seed.group_with_leader("Alpha")
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.create(scope=_scope(sa), payload=_create_payload(group_id=g1.id))
        assert await seed.account_group_id(dto.id) == g1.id

    async def test_super_admin_create_nonexistent_group_404(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        sa = await seed.super_admin()
        await db_session.commit()
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError) as ei:
            async with db_session.begin():
                await svc.create(scope=_scope(sa), payload=_create_payload(group_id=424242))
        assert ei.value.message == "group_not_found"

    async def test_resolve_target_user_id_not_weakened_member_on_other(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """ADR-0031 must NOT relax ADR-0019 §8: member→other owner rejected."""
        g1, _leader = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        other = await seed.member(g1.id)
        await db_session.commit()

        from backend.app.exceptions import ValidationError

        svc = MailAccountService(db_session)
        with pytest.raises(ValidationError):
            async with db_session.begin():
                await svc.create(
                    scope=_scope(member),
                    payload=_create_payload(target_user_id=other.id),
                )


# ===========================================================================
# TRANSFER — PATCH /api/mail-accounts/{id} with group_id (ADR-0031 §3/§4/§6)
# ===========================================================================


class TestTransfer:
    async def test_super_admin_valid_transfer_changes_group(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        member = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g1.id, email="b@x.com")
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.update(
                scope=_scope(sa),
                account_id=acc.id,
                payload=MailAccountUpdateRequest(group_id=g2.id),
            )
        assert await seed.account_group_id(acc.id) == g2.id

    async def test_transfer_visibility_follows_new_team(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """After transfer, the box is visible to the new team, not the old."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, leader2 = await seed.group_with_leader("Beta")
        owner = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=owner.id, group_id=g1.id, email="b@x.com")
        alpha_only = await seed.member(g1.id)
        sa = await seed.super_admin()
        acc_id = acc.id
        g1_id, g2_id = g1.id, g2.id
        # Snapshot scopes up front — ORM rows expire after commit/rollback below.
        leader2_scope = _scope(leader2, frozenset({g2_id}))
        sa_scope = _scope(sa)
        alpha_scope = _scope(alpha_only, frozenset({g1_id}))
        await db_session.commit()

        svc = MailAccountService(db_session)
        # Before: leader2 (team Beta) cannot see it.
        with pytest.raises(NotFoundError):
            await svc.get_for_scope(leader2_scope, acc_id)
        # Close the autobegun read-tx so the service can open its own write-tx.
        await db_session.rollback()
        async with db_session.begin():
            await svc.update(
                scope=sa_scope,
                account_id=acc_id,
                payload=MailAccountUpdateRequest(group_id=g2_id),
            )
        # After: leader2 sees it; an Alpha-only member no longer does.
        seen = await svc.get_for_scope(leader2_scope, acc_id)
        assert seen.id == acc_id
        await db_session.rollback()
        with pytest.raises(NotFoundError):
            await svc.get_for_scope(alpha_scope, acc_id)

    async def test_leader_transfer_within_scope(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Leader of g1 who is also a member of g2 can move a box g1→g2."""
        g1, leader = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        await seed.add_membership(user_id=leader.id, group_id=g2.id)
        # The box belongs to the leader themselves.
        acc = await seed.mail_account(user_id=leader.id, group_id=g1.id, email="b@x.com")
        await db_session.commit()

        scope = _scope(leader, group_ids=frozenset({g1.id, g2.id}))
        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.update(
                scope=scope,
                account_id=acc.id,
                payload=MailAccountUpdateRequest(group_id=g2.id),
            )
        assert await seed.account_group_id(acc.id) == g2.id

    async def test_group_member_transfer_forbidden_even_when_visible(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """A member sending group_id in PATCH → 403, even on a box they see."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g1.id, email="b@x.com")
        await db_session.commit()

        svc = MailAccountService(db_session)
        with pytest.raises(ForbiddenError) as ei:
            async with db_session.begin():
                await svc.update(
                    scope=_scope(member),
                    account_id=acc.id,
                    payload=MailAccountUpdateRequest(group_id=g1.id),
                )
        assert ei.value.message == "forbidden"
        assert ei.value.status_code == 403

    async def test_transfer_box_invisible_to_initiator_404(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Transferring a box the initiator cannot see → 404 (before authz)."""
        g1, leader1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        owner2 = await seed.member(g2.id)
        # Box lives in g2; leader1 has no membership there.
        acc = await seed.mail_account(user_id=owner2.id, group_id=g2.id, email="b@x.com")
        await db_session.commit()

        scope = _scope(leader1, group_ids=frozenset({g1.id}))
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError):
            async with db_session.begin():
                await svc.update(
                    scope=scope,
                    account_id=acc.id,
                    payload=MailAccountUpdateRequest(group_id=g1.id),
                )

    async def test_leader_transfer_into_disallowed_team_forbidden(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Leader moving a *member's* box into a non-home team → 403."""
        g1, leader = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        member = await seed.member(g1.id)
        await seed.add_membership(user_id=leader.id, group_id=g2.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g1.id, email="b@x.com")
        await db_session.commit()

        scope = _scope(leader, group_ids=frozenset({g1.id, g2.id}))
        svc = MailAccountService(db_session)
        with pytest.raises(ForbiddenError) as ei:
            async with db_session.begin():
                await svc.update(
                    scope=scope,
                    account_id=acc.id,
                    payload=MailAccountUpdateRequest(group_id=g2.id),
                )
        assert ei.value.message == "user_not_in_group_scope"

    async def test_leader_transfer_into_nonexistent_team_404(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, leader = await seed.group_with_leader("Alpha")
        acc = await seed.mail_account(user_id=leader.id, group_id=g1.id, email="b@x.com")
        await db_session.commit()
        svc = MailAccountService(db_session)
        with pytest.raises(NotFoundError) as ei:
            async with db_session.begin():
                await svc.update(
                    scope=_scope(leader, frozenset({g1.id})),
                    account_id=acc.id,
                    payload=MailAccountUpdateRequest(group_id=999_001),
                )
        assert ei.value.message == "group_not_found"

    async def test_noop_transfer_writes_no_audit_and_no_change(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """target == current → no audit row written."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g1.id, email="b@x.com")
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.update(
                scope=_scope(sa),
                account_id=acc.id,
                payload=MailAccountUpdateRequest(group_id=g1.id),  # same team
            )
        assert await seed.account_group_id(acc.id) == g1.id
        assert await seed.audit_rows("mail_account_group_change") == []

    async def test_transfer_writes_audit_row_with_details(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """Real transfer → audit row actor=initiator, target=owner, details."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        owner = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=owner.id, group_id=g1.id, email="b@x.com")
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.update(
                scope=_scope(sa),
                account_id=acc.id,
                payload=MailAccountUpdateRequest(group_id=g2.id),
            )
        rows = await seed.audit_rows("mail_account_group_change")
        assert len(rows) == 1
        row = rows[0]
        assert row["actor_user_id"] == sa.id
        assert row["target_user_id"] == owner.id
        assert row["details"] == {
            "mail_account_id": acc.id,
            "from_group_id": g1.id,
            "to_group_id": g2.id,
        }

    async def test_sentinel_patch_without_group_id_leaves_team_untouched(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """PATCH with no group_id key → team unchanged, no audit."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=member.id, group_id=g1.id, email="b@x.com")
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            # display_name change only — group_id key absent.
            await svc.update(
                scope=_scope(member),
                account_id=acc.id,
                payload=MailAccountUpdateRequest(display_name="Nick"),
            )
        assert await seed.account_group_id(acc.id) == g1.id
        assert await seed.audit_rows("mail_account_group_change") == []

    async def test_sentinel_set_group_id_inferred_from_json_key(self) -> None:
        """Schema: presence of group_id key flips set_group_id (even null)."""
        present = MailAccountUpdateRequest.model_validate({"group_id": None})
        assert present.set_group_id is True
        absent = MailAccountUpdateRequest.model_validate({"display_name": "x"})
        assert absent.set_group_id is False

    async def test_super_admin_transfer_to_null_detaches_team(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        """super_admin PATCH group_id=null → personal box (NULL)."""
        g1, _l1 = await seed.group_with_leader("Alpha")
        owner = await seed.member(g1.id)
        acc = await seed.mail_account(user_id=owner.id, group_id=g1.id, email="b@x.com")
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            await svc.update(
                scope=_scope(sa),
                account_id=acc.id,
                payload=MailAccountUpdateRequest.model_validate({"group_id": None}),
            )
        assert await seed.account_group_id(acc.id) is None
        rows = await seed.audit_rows("mail_account_group_change")
        assert rows[0]["details"]["to_group_id"] is None

    async def test_oauth_account_transfer_no_forbidden_no_retest(
        self, db_session: AsyncSession, seed: Seeder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OAuth box transfers without forbidden_changes / IMAP-SMTP retest."""
        from backend.app.accounts import service as svc_mod

        # If any IMAP/SMTP test runs during a pure transfer, blow up.
        async def _boom(**_: Any) -> None:
            raise AssertionError("transfer must not trigger an IMAP/SMTP test")

        monkeypatch.setattr(svc_mod, "imap_test_login", _boom)
        monkeypatch.setattr(svc_mod, "smtp_test_login", _boom)

        g1, _l1 = await seed.group_with_leader("Alpha")
        g2, _l2 = await seed.group_with_leader("Beta")
        owner = await seed.member(g1.id)
        acc = await seed.mail_account(
            user_id=owner.id, group_id=g1.id, email="ms@x.com", auth_type="oauth_outlook"
        )
        sa = await seed.super_admin()
        await db_session.commit()

        svc = MailAccountService(db_session)
        async with db_session.begin():
            dto = await svc.update(
                scope=_scope(sa),
                account_id=acc.id,
                payload=MailAccountUpdateRequest(group_id=g2.id),
            )
        assert dto.auth_type == "oauth_outlook"
        assert await seed.account_group_id(acc.id) == g2.id


# ===========================================================================
# GET /api/my/groups — GroupsService.selectable_teams (ADR-0031 §5)
# ===========================================================================


class TestSelectableTeams:
    async def test_member_sees_only_own_teams_with_home(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _l1 = await seed.group_with_leader("Beta")
        g2, _l2 = await seed.group_with_leader("Alpha")
        member = await seed.member(g1.id)
        await seed.add_membership(user_id=member.id, group_id=g2.id)
        await db_session.commit()

        scope = _scope(member, group_ids=frozenset({g1.id, g2.id}))
        out = await GroupsService(db_session).selectable_teams(scope)
        # Sorted by name: Alpha (g2) then Beta (g1).
        assert [g.name for g in out.groups] == ["Alpha", "Beta"]
        assert {g.id for g in out.groups} == {g1.id, g2.id}
        assert out.home_group_id == g1.id

    async def test_leader_sees_own_teams(self, db_session: AsyncSession, seed: Seeder) -> None:
        g1, leader = await seed.group_with_leader("Gamma")
        await db_session.commit()
        out = await GroupsService(db_session).selectable_teams(_scope(leader, frozenset({g1.id})))
        assert [g.id for g in out.groups] == [g1.id]
        assert out.home_group_id == g1.id

    async def test_super_admin_sees_all_groups_home_null(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _l1 = await seed.group_with_leader("Zeta")
        g2, _l2 = await seed.group_with_leader("Aurora")
        sa = await seed.super_admin()
        await db_session.commit()

        out = await GroupsService(db_session).selectable_teams(_scope(sa))
        # All groups, sorted by name.
        assert [g.name for g in out.groups] == ["Aurora", "Zeta"]
        assert {g.id for g in out.groups} == {g1.id, g2.id}
        assert out.home_group_id is None

    async def test_response_shape_sorted_by_name(
        self, db_session: AsyncSession, seed: Seeder
    ) -> None:
        g1, _l1 = await seed.group_with_leader("Bravo")
        g2, _l2 = await seed.group_with_leader("Charlie")
        g3, _l3 = await seed.group_with_leader("Alpha")
        sa = await seed.super_admin()
        await db_session.commit()
        out = await GroupsService(db_session).selectable_teams(_scope(sa))
        names = [g.name for g in out.groups]
        assert names == sorted(names)
        dumped = out.model_dump()
        assert set(dumped.keys()) == {"groups", "home_group_id"}
        assert set(dumped["groups"][0].keys()) == {"id", "name"}
        _ = (g1, g2, g3)  # referenced for clarity
