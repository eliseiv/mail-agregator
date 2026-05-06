"""SQLAlchemy ORM models, one per table.

Schema mirrors ``docs/03-data-model.md`` exactly. Any drift here from
that document is a bug — fix the code, not the doc.
"""

from shared.models.admin_audit import AdminAudit
from shared.models.attachment import Attachment
from shared.models.mail_account import MailAccount
from shared.models.message import Message
from shared.models.sent_attachment import SentAttachment
from shared.models.sent_message import SentMessage
from shared.models.user import User

__all__ = [
    "AdminAudit",
    "Attachment",
    "MailAccount",
    "Message",
    "SentAttachment",
    "SentMessage",
    "User",
]
