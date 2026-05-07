"""Parameterised SQL used by the tags module (ADR-0017 §5/§7).

Two queries:

* :data:`APPLY_TAGS_TO_MESSAGE` — given a single ``message_id`` plus the
  pre-resolved subject / body / sender / user_id, INSERT one row in
  ``message_tags`` for every tag of that user whose rules match. Called
  from the worker (``worker.app.sync_cycle``) after each successful
  ``insert_message_idempotent`` and from the service when a fresh tag is
  created with ``apply_to_existing=true`` is replayed for a single
  message (currently the worker path).

* :data:`APPLY_TAG_TO_EXISTING` — bulk INSERT every existing message of a
  user that matches the given ``tag_id``'s rules. Called from
  ``POST /api/tags`` (when ``apply_to_existing=true``) and
  ``POST /api/tags/{id}/apply-to-existing``.

Both queries are idempotent (``ON CONFLICT (message_id, tag_id) DO NOTHING``).
ILIKE is linear — no ReDoS risk (see ADR-0017 §4 / Alternatives A2).
"""

from __future__ import annotations

from typing import Final

# NB: parameters are positional named-binds via SQLAlchemy ``text(...)``;
# ``sender`` is used twice in the ``sender_exact`` branch so we render it
# once and let SQLAlchemy reuse the bind on both sides of the comparison.
APPLY_TAGS_TO_MESSAGE: Final[str] = """
INSERT INTO message_tags (message_id, tag_id)
SELECT :message_id, t.id
FROM tags t
WHERE t.user_id = :user_id
  AND EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id AND (
        (r.type = 'subject_contains' AND :subject ILIKE '%' || r.pattern || '%') OR
        (r.type = 'body_contains'    AND :body    ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_contains'  AND :sender  ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_exact'     AND LOWER(:sender) = LOWER(r.pattern))
    )
  )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""


APPLY_TAG_TO_EXISTING: Final[str] = """
INSERT INTO message_tags (message_id, tag_id)
SELECT m.id, :tag_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE ma.user_id = :user_id
  AND EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id AND (
        (r.type = 'subject_contains' AND m.subject   ILIKE '%' || r.pattern || '%') OR
        (r.type = 'body_contains'    AND m.body_text ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_contains'  AND m.from_addr ILIKE '%' || r.pattern || '%') OR
        (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
    )
  )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""
