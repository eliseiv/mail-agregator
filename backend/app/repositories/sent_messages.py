"""Repository for ``sent_messages``."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import SentMessage


class SentMessagesRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(
        self,
        *,
        user_id: int,
        from_account_id: int,
        to_addrs: str,
        cc_addrs: str | None,
        bcc_addrs: str | None,
        subject: str | None,
        body_text: str,
        in_reply_to: str | None,
        refs_header: str | None,
        smtp_message_id: str,
        appended_to_sent: bool,
        appended_error: str | None,
    ) -> SentMessage:
        sm = SentMessage(
            user_id=user_id,
            from_account_id=from_account_id,
            to_addrs=to_addrs,
            cc_addrs=cc_addrs,
            bcc_addrs=bcc_addrs,
            subject=subject,
            body_text=body_text,
            in_reply_to=in_reply_to,
            refs_header=refs_header,
            smtp_message_id=smtp_message_id,
            appended_to_sent=appended_to_sent,
            appended_error=appended_error,
        )
        self._s.add(sm)
        await self._s.flush()
        await self._s.refresh(sm)
        return sm
