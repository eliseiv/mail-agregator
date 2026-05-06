"""Mail send module: compose / reply, SMTP send, IMAP append best-effort."""

from backend.app.send.router import router

__all__ = ["router"]
