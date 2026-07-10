"""Headless-OAuth CRM notification (ADR-0045 §3).

After a successful Outlook create/relink in the external callback, POST the new
mailbox↔team binding to the CRM ingest endpoint
(``CRM_OAUTH_INGEST_URL`` = CRM ``/api/mail/oauth/ingest``) with the SAME
HMAC-SHA256 scheme + reused secret (``CRM_PUSH_SECRET``) as ``/api/mail/ingest``
(ADR-0043 §2 / ``backend/app/crm_push/service.py``): the JSON body is serialised
ONCE and those exact bytes are both signed and sent (``content=raw_body``, never
``json=``), with headers ``X-Mail-Signature: sha256=<hex>`` + ``X-Mail-Timestamp``
over the canonical ``str(ts).encode("ascii") + b"." + raw_body`` (reusing
:func:`backend.app.crm_push.service.build_signature`).

Best-effort (ADR-0045 §3): a delivery failure NEVER rolls back the already
created mailbox — the CRM reconcile job backfills (CRM TD-047). Retry is
CONNECT-ONLY: we retry solely when the TCP connection was never established (so
the request cannot have been processed), and otherwise stop — the CRM upsert is
idempotent by ``mail_account_id``, so a single successful delivery is enough and
a re-send after the CRM has already seen the bytes is avoided (anti-double-write).

``CRM_OAUTH_INGEST_URL`` / ``CRM_PUSH_SECRET`` are never logged.
"""

from __future__ import annotations

import json
import time
from typing import Final

import httpx

from backend.app.crm_push.service import build_signature
from shared.config import get_settings
from shared.logging import get_logger

log = get_logger(__name__)

# Connect-only retry budget: total attempts when the connection never opened.
_CONNECT_RETRY_ATTEMPTS: Final[int] = 3


def _serialize(body: dict[str, object]) -> bytes:
    """Serialise the JSON body ONCE — these exact bytes are signed AND sent."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def notify_crm_oauth_ingest(
    *,
    crm_state: str,
    mail_account_id: int,
    email: str,
    display_name: str | None,
    is_active: bool,
) -> bool:
    """POST the created/relinked mailbox binding to the CRM ingest (ADR-0045 §3).

    Returns ``True`` on a ``2xx`` delivery, ``False`` otherwise (disabled,
    transport error, non-2xx). Never raises — the caller treats the result as
    advisory (the mailbox already exists regardless).
    """
    settings = get_settings()
    if not settings.crm_oauth_ingest_enabled:
        # URL and/or shared secret not configured — headless-OAuth ingest off.
        log.info("oauth_ingest_disabled", mail_account_id=mail_account_id)
        return False

    body: dict[str, object] = {
        "crm_state": crm_state,
        "mail_account_id": int(mail_account_id),
        "email": email,
        "display_name": display_name,
        "is_active": bool(is_active),
    }
    raw_body = _serialize(body)
    ts = int(time.time())
    signature = build_signature(settings.CRM_PUSH_SECRET, ts, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Mail-Signature": f"sha256={signature}",
        "X-Mail-Timestamp": str(ts),
    }
    timeout = httpx.Timeout(float(settings.CRM_PUSH_HTTP_TIMEOUT_SECONDS))

    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, verify=True
            ) as client:
                resp = await client.post(
                    settings.CRM_OAUTH_INGEST_URL, content=raw_body, headers=headers
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Connection never established → the request cannot have been
            # processed, so retrying is double-write-safe (ADR-0045 §3).
            log.warning(
                "oauth_ingest_connect_error",
                mail_account_id=mail_account_id,
                attempt=attempt,
                error_type=type(exc).__name__,
            )
            continue
        except httpx.HTTPError as exc:
            # Any other transport error (e.g. a read timeout) may mean the CRM
            # already received the body — do NOT retry (anti-double-write).
            log.warning(
                "oauth_ingest_transport_error",
                mail_account_id=mail_account_id,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )
            return False

        if 200 <= resp.status_code < 300:
            log.info(
                "oauth_ingest_delivered",
                mail_account_id=mail_account_id,
                status=resp.status_code,
            )
            return True
        # Got a response — the CRM saw the request; a retry would double-write
        # (upsert is idempotent, but we still stop and rely on reconcile).
        log.warning(
            "oauth_ingest_rejected",
            mail_account_id=mail_account_id,
            status=resp.status_code,
            body_excerpt=resp.text[:200],
        )
        return False

    log.warning("oauth_ingest_connect_exhausted", mail_account_id=mail_account_id)
    return False
