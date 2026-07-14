"""SQLAlchemy ORM models, one per table.

Schema mirrors ``docs/03-data-model.md`` exactly. Any drift here from
that document is a bug — fix the code, not the doc.

ADR-0044 (phase A3): the ORM mapping of the decommissioned subsystems (tags,
Telegram, webhooks, forwarding, groups/memberships, audit, attachments,
user-settings) is removed BEFORE the DDL (§3 lock-step). What stays:
``mail_accounts``, ``messages``, ``users`` (technical, the ``crm-service`` row)
plus ``sent_messages`` / ``sent_attachments`` until the generic send lands
(§4, phase A2).
"""

from shared.models.mail_account import MailAccount
from shared.models.message import Message
from shared.models.sent_attachment import SentAttachment
from shared.models.sent_message import SentMessage
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
    "SentAttachment",
    "SentMessage",
    "User",
]
