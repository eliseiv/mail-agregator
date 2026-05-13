"""SQLAlchemy ORM models, one per table.

Schema mirrors ``docs/03-data-model.md`` exactly. Any drift here from
that document is a bug — fix the code, not the doc.
"""

from shared.models.admin_audit import AdminAudit
from shared.models.attachment import Attachment
from shared.models.group import Group
from shared.models.mail_account import MailAccount
from shared.models.message import Message
from shared.models.sent_attachment import SentAttachment
from shared.models.sent_message import SentMessage
from shared.models.tag import MessageTag, Tag, TagRule
from shared.models.telegram_link import TelegramLink
from shared.models.telegram_notification import TelegramNotification
from shared.models.user import (
    ALL_ROLES,
    ROLE_GROUP_LEADER,
    ROLE_GROUP_MEMBER,
    ROLE_SUPER_ADMIN,
    User,
)
from shared.models.user_settings import UserSettings

__all__ = [
    "ALL_ROLES",
    "ROLE_GROUP_LEADER",
    "ROLE_GROUP_MEMBER",
    "ROLE_SUPER_ADMIN",
    "AdminAudit",
    "Attachment",
    "Group",
    "MailAccount",
    "Message",
    "MessageTag",
    "SentAttachment",
    "SentMessage",
    "Tag",
    "TagRule",
    "TelegramLink",
    "TelegramNotification",
    "User",
    "UserSettings",
]
