"""WebhooksService — CRUD + rotate-secret + send-test (ADR-0023 §2).

All public methods accept the caller's :class:`VisibilityScope` and
``ip`` / ``user_agent`` strings; they enforce authorisation themselves
(``group_leader`` → own group, ``super_admin`` → any group via the
``group_id`` parameter, ``group_member`` → 403).

The service does **not** open transactions — the router wraps every
mutating call in ``async with db.begin():`` so the audit write commits
atomically with the business row.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.audit import AuditWriter
from backend.app.deps import VisibilityScope
from backend.app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    WebhookUrlPrivateIpError,
)
from backend.app.repositories.webhooks import WebhooksRepo
from backend.app.webhooks.schemas import (
    WebhookCreatedDTO,
    WebhookDTO,
    WebhookTestResult,
)
from shared.config import get_settings
from shared.crypto import encrypt_webhook_secret
from shared.logging import get_logger
from shared.models import Webhook
from shared.url_safety import WebhookUrlError, validate_outbound_url

log = get_logger(__name__)

# Test-endpoint POST headers (ADR-0023 §2.8).
_TEST_HEADERS_TEMPLATE: dict[str, str] = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "mas-webhook/1.0",
    "X-Webhook-Event": "test",
    "X-Webhook-Delivery-Id": "00000000",
}


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    """Result of :meth:`WebhooksService._resolve_target_group_id`.

    Wrapping the two relevant pieces of state (the actor's role context
    and the target group_id) keeps the per-method authorisation code
    short and explicit.
    """

    group_id: int
    target_user_id: int | None  # leader of the resolved group (for audit)


def _to_dto(webhook: Webhook) -> WebhookDTO:
    return WebhookDTO(
        id=webhook.id,
        group_id=webhook.group_id,
        url=webhook.url,
        is_active=webhook.is_active,
        consecutive_failures=webhook.consecutive_failures,
        dead_at=webhook.dead_at,
        last_fired_at=webhook.last_fired_at,
        last_error=webhook.last_error,
        created_at=webhook.created_at,
        updated_at=webhook.updated_at,
    )


def _validate_url_or_raise(url: str) -> str:
    """Run the SSRF + lexical check and translate failures into the right
    domain-error envelope."""
    try:
        return validate_outbound_url(url)
    except WebhookUrlError as exc:
        if exc.reason == "webhook_url_private_ip":
            raise WebhookUrlPrivateIpError(str(exc)) from exc
        # All other reasons (scheme, length, dns_failed, ...) → 400
        # validation_error so the frontend can surface the message
        # next to the URL input.
        raise ValidationError(str(exc), field="url") from exc


class WebhooksService:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session
        self._repo = WebhooksRepo(session)
        self._audit = AuditWriter(session)

    # --- Authorisation helpers --------------------------------------------

    def _resolve_target_group_id(
        self, scope: VisibilityScope, *, override_group_id: int | None
    ) -> _ResolvedTarget:
        """Decide which group_id this call should act on.

        Rules (ADR-0023 §2.1):

        - ``super_admin`` MUST pass ``override_group_id`` (a query
          parameter); otherwise 400 ``validation_error``.
        - ``group_leader`` MUST NOT pass ``override_group_id``; if
          they do, 400 ``validation_error``. The leader can only act on
          their own ``scope.group_id``.
        - ``group_member`` is always 403.
        """
        if scope.is_group_member:
            raise ForbiddenError("group members cannot manage webhooks")

        if scope.is_super_admin:
            if override_group_id is None:
                raise ValidationError(
                    "super_admin must pass ?group_id=<int>",
                    field="group_id",
                )
            return _ResolvedTarget(group_id=override_group_id, target_user_id=None)

        # group_leader path.
        if override_group_id is not None:
            raise ValidationError(
                "group_leader cannot pass ?group_id=<int>",
                field="group_id",
            )
        if scope.group_id is None:
            # Defensive — a leader without a group is a data-model
            # inconsistency, but surfacing as 404 is correct (we have
            # nothing to manage).
            raise NotFoundError("caller has no group")
        return _ResolvedTarget(group_id=scope.group_id, target_user_id=scope.user_id)

    # --- Reads ------------------------------------------------------------

    async def get_for_scope(
        self, scope: VisibilityScope, *, override_group_id: int | None
    ) -> WebhookDTO:
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        webhook = await self._repo.get_by_group_id(target.group_id)
        if webhook is None:
            raise NotFoundError("webhook is not configured for this group")
        return _to_dto(webhook)

    # --- Writes -----------------------------------------------------------

    async def create_for_scope(
        self,
        scope: VisibilityScope,
        *,
        url: str,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> WebhookCreatedDTO:
        """Create the webhook + emit secret in plaintext (one-time-show).

        Algorithm (ADR-0023 §2.2):

        1. Resolve the target group and validate the URL.
        2. Refuse if a row already exists (UNIQUE on ``group_id``).
        3. Reserve the next BIGSERIAL id; encrypt secret with AAD bound
           to that id; INSERT with explicit id.
        4. Write audit ``webhook_created``.
        """
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        validated_url = _validate_url_or_raise(url)

        existing = await self._repo.get_by_group_id(target.group_id)
        if existing is not None:
            raise ConflictError("webhook already exists for this group", field="group_id")

        webhook_id = await self._repo.next_webhook_id()
        secret_plaintext = secrets.token_urlsafe(32)
        secret_encrypted = encrypt_webhook_secret(secret_plaintext, webhook_id)

        webhook = await self._repo.insert_with_id(
            webhook_id=webhook_id,
            group_id=target.group_id,
            url=validated_url,
            secret_encrypted=secret_encrypted,
        )

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="webhook_created",
            target_user_id=target.target_user_id,
            details={
                "webhook_id": webhook.id,
                "group_id": webhook.group_id,
                "url": validated_url,
            },
            ip=ip,
            user_agent=user_agent,
        )

        log.info(
            "webhook_created",
            webhook_id=webhook.id,
            group_id=webhook.group_id,
            actor_user_id=scope.user_id,
        )

        return WebhookCreatedDTO(
            **_to_dto(webhook).model_dump(),
            secret=secret_plaintext,
        )

    async def update_for_scope(
        self,
        scope: VisibilityScope,
        *,
        url: str | None,
        is_active: bool | None,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> WebhookDTO:
        """PATCH webhook fields.

        At least one of ``url`` / ``is_active`` must be provided (else
        400). ``is_active=true`` from a dead state additionally resets
        ``dead_at`` / ``consecutive_failures`` / ``last_error``
        (re-enable). ``is_active=false`` simply flips the flag; the row
        + delivery history are preserved.
        """
        if url is None and is_active is None:
            raise ValidationError("at least one of url, is_active must be set")

        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        webhook = await self._repo.get_by_group_id(target.group_id)
        if webhook is None:
            raise NotFoundError("webhook is not configured for this group")

        changed_fields: list[str] = []
        previous_dead_at = webhook.dead_at

        validated_url: str | None = None
        if url is not None and url != webhook.url:
            validated_url = _validate_url_or_raise(url)
            changed_fields.append("url")
        elif url is not None:
            # Same URL submitted — keep behaviour idempotent (no audit
            # for a non-change).
            pass

        will_revive_dead = is_active is True and webhook.dead_at is not None

        if is_active is not None and is_active != webhook.is_active:
            changed_fields.append("is_active")
        elif will_revive_dead:
            # ``is_active=true`` on an already-active-but-dead row still
            # counts as a re-enable.
            changed_fields.append("is_active")

        if not changed_fields:
            # Nothing changed — no audit, no UPDATE.
            return _to_dto(webhook)

        # Apply changes. ``mark_alive`` covers the "re-enable a dead
        # webhook" case in one statement; if only the URL is changing we
        # do a generic update.
        if will_revive_dead:
            await self._repo.mark_alive(webhook.id)
            if validated_url is not None:
                await self._repo.update_fields(webhook.id, url=validated_url)
            if is_active is False:
                # Defensive — should not happen due to the gate above.
                await self._repo.update_fields(webhook.id, is_active=False)
        else:
            fields: dict[str, object] = {}
            if validated_url is not None:
                fields["url"] = validated_url
            if is_active is not None and is_active != webhook.is_active:
                fields["is_active"] = is_active
            if fields:
                await self._repo.update_fields(webhook.id, **fields)

        refreshed = await self._repo.get_by_id(webhook.id)
        if refreshed is None:
            # Concurrent delete — surface 404 so caller knows to refetch.
            raise NotFoundError("webhook was removed concurrently")

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="webhook_updated",
            target_user_id=target.target_user_id,
            details={
                "webhook_id": refreshed.id,
                "changed_fields": changed_fields,
                "previous_dead_at": previous_dead_at.isoformat()
                if previous_dead_at is not None
                else None,
            },
            ip=ip,
            user_agent=user_agent,
        )

        log.info(
            "webhook_updated",
            webhook_id=refreshed.id,
            group_id=refreshed.group_id,
            changed_fields=changed_fields,
            actor_user_id=scope.user_id,
        )

        return _to_dto(refreshed)

    async def delete_for_scope(
        self,
        scope: VisibilityScope,
        *,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> None:
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        webhook = await self._repo.get_by_group_id(target.group_id)
        if webhook is None:
            raise NotFoundError("webhook is not configured for this group")

        webhook_id = webhook.id
        group_id = webhook.group_id
        url = webhook.url

        await self._repo.delete(webhook_id)

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="webhook_deleted",
            target_user_id=target.target_user_id,
            details={
                "webhook_id": webhook_id,
                "group_id": group_id,
                "url": url,
            },
            ip=ip,
            user_agent=user_agent,
        )

        log.info(
            "webhook_deleted",
            webhook_id=webhook_id,
            group_id=group_id,
            actor_user_id=scope.user_id,
        )

    async def rotate_secret_for_scope(
        self,
        scope: VisibilityScope,
        *,
        override_group_id: int | None,
        ip: str | None,
        user_agent: str | None,
    ) -> WebhookCreatedDTO:
        """Generate a new secret, replace the encrypted blob, return the
        new plaintext (one-time-show)."""
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        webhook = await self._repo.get_by_group_id(target.group_id)
        if webhook is None:
            raise NotFoundError("webhook is not configured for this group")

        new_secret_plaintext = secrets.token_urlsafe(32)
        new_secret_encrypted = encrypt_webhook_secret(new_secret_plaintext, webhook.id)

        await self._repo.update_secret(webhook.id, new_secret_encrypted)

        refreshed = await self._repo.get_by_id(webhook.id)
        if refreshed is None:
            raise NotFoundError("webhook was removed concurrently")

        await self._audit.log(
            actor_user_id=scope.user_id,
            action="webhook_secret_rotated",
            target_user_id=target.target_user_id,
            details={"webhook_id": refreshed.id},
            ip=ip,
            user_agent=user_agent,
        )

        log.info(
            "webhook_secret_rotated",
            webhook_id=refreshed.id,
            group_id=refreshed.group_id,
            actor_user_id=scope.user_id,
        )

        return WebhookCreatedDTO(
            **_to_dto(refreshed).model_dump(),
            secret=new_secret_plaintext,
        )

    async def send_test(
        self,
        scope: VisibilityScope,
        *,
        override_group_id: int | None,
    ) -> WebhookTestResult:
        """Synchronous test POST.

        Does **not** touch ``webhook_deliveries``, ``consecutive_failures``,
        ``last_fired_at`` or ``last_error`` — purely a diagnostic call.
        Returns 200 with the receiver's status_code / excerpt even on
        receiver 5xx; only transport-level failure (DNS, timeout, network
        unreachable) yields the ``"network"`` status branch.

        Decrypts the secret on the fly; if decrypt fails the webhook is
        unusable and we return ``dns_failed`` semantics so the UI can
        prompt the lead to rotate. Decrypt failure does NOT mark dead
        here — :meth:`WebhookDispatchService.dispatch_one_payload` is
        the right place for that escalation.
        """
        target = self._resolve_target_group_id(scope, override_group_id=override_group_id)
        webhook = await self._repo.get_by_group_id(target.group_id)
        if webhook is None:
            raise NotFoundError("webhook is not configured for this group")

        # Re-validate URL (cache-poison defence — DNS records may have
        # changed since the row was inserted).
        try:
            validated_url = validate_outbound_url(webhook.url)
        except WebhookUrlError as exc:
            return WebhookTestResult(
                status="dns_failed" if exc.reason == "dns_failed" else "network",
                response_code=None,
                response_excerpt=None,
                duration_ms=0,
                detail=str(exc)[:200],
            )

        # Decrypt the secret right before the POST so the plaintext lives
        # in memory for the absolute minimum time.
        from shared.crypto import InvalidTag, decrypt_webhook_secret

        try:
            secret_plaintext = decrypt_webhook_secret(webhook.secret_encrypted, webhook.id)
        except InvalidTag:
            return WebhookTestResult(
                status="network",
                response_code=None,
                response_excerpt=None,
                duration_ms=0,
                detail="secret_decrypt_failed",
            )

        settings = get_settings()
        payload = {
            "event": "test",
            "timestamp": _utc_now_iso(),
            "webhook_id": webhook.id,
            "team": {"id": webhook.group_id},
        }
        headers = dict(_TEST_HEADERS_TEMPLATE)
        headers["X-Webhook-Secret"] = secret_plaintext

        timeout = httpx.Timeout(float(settings.WEBHOOK_HTTP_TIMEOUT_SECONDS))
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, verify=True
            ) as client:
                response = await client.post(validated_url, json=payload, headers=headers)
            duration_ms = int((time.monotonic() - start) * 1000)
            excerpt = response.text[:500] if response.text else ""
            status_label = "ok" if 200 <= response.status_code < 300 else "http_error"
            log.info(
                "webhook_test_sent",
                webhook_id=webhook.id,
                group_id=webhook.group_id,
                response_code=response.status_code,
                duration_ms=duration_ms,
            )
            return WebhookTestResult(
                status=status_label,
                response_code=response.status_code,
                response_excerpt=excerpt,
                duration_ms=duration_ms,
            )
        except httpx.TimeoutException:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info(
                "webhook_test_timeout",
                webhook_id=webhook.id,
                group_id=webhook.group_id,
                duration_ms=duration_ms,
            )
            return WebhookTestResult(
                status="network",
                response_code=None,
                response_excerpt=None,
                duration_ms=duration_ms,
                detail="timeout",
            )
        except httpx.RequestError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            log.info(
                "webhook_test_network_error",
                webhook_id=webhook.id,
                group_id=webhook.group_id,
                error_type=type(exc).__name__,
                duration_ms=duration_ms,
            )
            return WebhookTestResult(
                status="network",
                response_code=None,
                response_excerpt=None,
                duration_ms=duration_ms,
                detail=type(exc).__name__,
            )


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
