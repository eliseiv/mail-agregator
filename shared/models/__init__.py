"""SQLAlchemy ORM models, one per table.

Schema mirrors ``docs/03-data-model.md`` exactly. Any drift here from
that document is a bug — fix the code, not the doc.

ADR-0044 (phase A3): the ORM mapping of the decommissioned subsystems (tags,
Telegram, webhooks, forwarding, groups/memberships, audit, attachments,
user-settings) is removed BEFORE the DDL (§3 lock-step). ADR-0048 §3 (phase
A2.2): ``sent_messages`` / ``sent_attachments`` ORM classes were removed once the
CRM was confirmed on the generic send and the reply writer was dropped — their
DROP TABLE follows in the DDL phase (D). What stays: ``mail_accounts``,
``messages``, ``users`` (technical, the ``crm-service`` row).
"""

from shared.models.mail_account import MailAccount
from shared.models.message import Message
from shared.models.user import (
    ALL_ROLES,
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
    User,
)

__all__ = [
    "ALL_ROLES",
    "ROLE_GROUP_LEADER",
    "ROLE_GROUP_MEMBER",
    "ROLE_SUPER_ADMIN",
    "MailAccount",
    "Message",
    "User",
]
