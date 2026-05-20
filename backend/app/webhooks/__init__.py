"""Outbound webhooks module (ADR-0023).

- ``schemas.py``         — Pydantic DTOs for the CRUD endpoints.
- ``service.py``         — :class:`WebhooksService` (CRUD + rotate + test).
- ``dispatch_service.py``— :class:`WebhookDispatchService` (enqueue +
  per-message POST + dead-mark / retry logic).
- ``router.py``          — HTTP routes (``/api/webhooks/me*`` + HTML).
"""
