"""ADR-0044 Phase D — drop 15 of the 16 decommissioned tables.

Runbook: ``docs/adr/ADR-0044-decommission-runbook.md`` §1 (DROP verdicts) / §4
(Phase D). ``groups`` is the 16th table and is dropped separately in Phase E
(its incoming FK columns — ``mail_accounts.group_id`` in Phase C,
``users.group_id`` in Phase E — must come off first).

The A-phase code detach (already deployed to prod) removed every ORM
class/repository/reader/writer for these tables, so the DDL is safe (ADR §3
lock-step). ``admin_audit`` is dropped LAST (position 15) and only AFTER the
mandatory ``pg_dump`` backup (§6 / Phase B — performed by the operator; here it
is a plain drop, TD-050).

**Drop order — strictly referencing → referenced** (verified against the live
FK map). Plain ``DROP TABLE`` (RESTRICT, no CASCADE) is used deliberately so a
wrong order fails loudly instead of silently cascading. No KEEP table
(``mail_accounts``, ``messages``, ``users``) references any table in this set
(``mail_accounts.group_id`` was already removed in Phase C).

``downgrade()`` is an explicit, loud no-op guard — see the module docstring
rationale below.

**Why downgrade does NOT structurally recreate these tables.** ADR-0044
declares Phases C–G the irreversible "точка невозврата"; §6 defines the
``pg_dump`` / MinIO backup — NOT ``alembic downgrade`` — as the recovery path.
The A-phase detach already deleted these 16 ORM models from ``shared/models/``,
so a structural recreation would produce empty tables the current codebase can
neither map nor use — false reversibility that could mask the real (backup)
recovery path. A loud ``RuntimeError`` with actionable guidance is safer and
more honest than either a lossy structural shell or a silent ``pass`` (per the
task's explicit "явная заглушка с понятным сообщением" allowance).

Revision ID: 20260715_026
Revises: 20260715_025
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op

revision = "20260715_026"
down_revision = "20260715_025"
branch_labels = None
depends_on = None

# Referencing → referenced. Order is normative (ADR §4 Phase D) — do not sort.
_DROP_ORDER: tuple[str, ...] = (
    "sent_attachments",  # → sent_messages
    "sent_messages",  # → users, mail_accounts
    "attachments",  # → messages
    "message_tags",  # → messages, tags
    "tag_rules",  # → tags
    "tags",  # → users
    "telegram_notifications",  # → messages, users
    "telegram_links",  # → users
    "webhook_deliveries",  # → webhooks, messages
    "webhooks",  # → groups
    "message_forwards",  # → messages, groups
    "group_forwarding",  # → groups
    "user_groups",  # → users, groups
    "users_settings",  # → users
    "admin_audit",  # no FK — dropped last, AFTER the §6 backup (TD-050)
)


def upgrade() -> None:
    for table in _DROP_ORDER:
        op.execute(f"DROP TABLE {table}")


def downgrade() -> None:
    raise RuntimeError(
        "ADR-0044 Phase D (drop of 15 aggregator tables) is an irreversible "
        "decommission step (point of no return, ADR §4). The dropped data is "
        "permanently gone and the ORM no longer maps these tables. Recovery is "
        "by restoring the §6 pg_dump backup, NOT by `alembic downgrade`. "
        "Refusing to fabricate an empty structural shell that would falsely "
        "imply reversibility."
    )
