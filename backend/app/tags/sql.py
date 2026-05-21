r"""Parameterised SQL used by the tags module (ADR-0017 §5/§7).

Two queries (both honour the round-10 team-visibility model — see
``docs/05-modules.md`` sec. 17 + ADR-0019 / ADR-0022 production patch):

* :data:`APPLY_TAGS_TO_MESSAGE` — given a single freshly-inserted
  ``message_id`` plus its ``mail_account_id`` and resolved subject / body
  / sender, INSERT one row in ``message_tags`` for every matching tag of
  every user who can SEE that message. A user sees a message when either
  (a) they own its mail account, or (b) the mail account's ``group_id``
  matches the user's ``group_id``. This is the worker hook called after
  ``insert_message_idempotent`` in ``worker.app.sync_cycle``.

* :data:`APPLY_TAG_TO_EXISTING` — bulk INSERT every existing message
  visible to the tag's owner that matches the given ``tag_id``'s rules.
  Visibility = personal accounts (``ma.user_id = :user_id``) plus the
  owner's team accounts (``ma.group_id = :user_group_id``). For a user
  without a group, pass ``:user_group_id = NULL`` and the second branch
  short-circuits. Called from ``POST /api/tags`` (when
  ``apply_to_existing=true``) and ``POST /api/tags/{id}/apply-to-existing``.

Both queries are idempotent (``ON CONFLICT (message_id, tag_id) DO NOTHING``).

round-23 (whole-word, case-sensitive matching): the three ``*_contains``
rule types (``subject_contains`` / ``body_contains`` / ``sender_contains``)
previously used substring ``ILIKE '%' || pattern || '%'`` (substring,
case-insensitive). That falsely matched a pattern as a *substring* of a
larger word — e.g. builtin pattern ``PLA`` matched "ex**pla**ining",
"tem**pla**te", "dis**pla**y", attaching the ``DPLA.PLA`` tag where it must
not. We now match on **whole words, case-SENSITIVELY** using the POSIX
case-sensitive regex operator ``~`` with ``\y`` word boundaries on both
sides::

    :body ~ ('\y' || <escaped_pattern> || '\y')

Case-sensitivity is deliberate: the **user controls the case** by what they
type into the pattern. If a rule pattern is ``DPLA`` (caps), only an exact
capitalised whole-word ``DPLA`` in the text matches — e.g. ``Program
Licence Agreement ("DPLA")`` matches, but a lowercase ``dpla`` does not.
This gives a double safeguard against false positives: wrong case (e.g.
``pla`` ≠ ``PLA``) *and* substring-inside-a-word (``\y`` boundaries) are
both rejected.

The user pattern is escaped with ``regexp_replace`` so every regex
metacharacter is treated literally (e.g. the ``.`` in ``DPLA.PLA`` matches a
literal dot, not "any char"). ``sender_exact`` is unchanged
(``LOWER(...) = LOWER(...)`` — email/domain matching is de-facto
case-insensitive, so it stays so on purpose).

This is bounded-linear in practice (anchored literal alternations over a
fixed pattern) — no user-supplied regex structure reaches the engine
because every metacharacter is escaped first, so the ReDoS argument of
ADR-0017 §4 / Alternatives A2 still holds. This changes the semantics of
``*_contains`` from substring to whole-word — see ADR-0017 update note and
``docs/100-known-tech-debt.md`` (round-23).
"""

from __future__ import annotations

from typing import Final

# NB: parameters are positional named-binds via SQLAlchemy ``text(...)``;
# ``sender`` is used twice in the ``sender_exact`` branch so we render it
# once and let SQLAlchemy reuse the bind on both sides of the comparison.
#
# Visibility join: a tag belongs to user ``t.user_id``; that user sees the
# new message iff either they own the mail account OR (ma.group_id IS NOT
# NULL AND u.group_id = ma.group_id). Both sides of the OR are needed
# because team accounts retain their original ``group_id`` even when the
# owner is moved to a different group (round-10 production patch).
# round-23: ``*_contains`` is whole-word, case-SENSITIVE (``~`` + ``\y``
# boundaries) over a regex-escaped pattern (literal match). The user
# controls case via the pattern they type. See module docstring.
APPLY_TAGS_TO_MESSAGE: Final[str] = r"""
INSERT INTO message_tags (message_id, tag_id)
SELECT :message_id, t.id
FROM tags t
JOIN users u ON u.id = t.user_id
JOIN mail_accounts ma ON ma.id = :mail_account_id
WHERE (
        u.id = ma.user_id
        OR (ma.group_id IS NOT NULL AND u.group_id = ma.group_id)
    )
  AND EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id AND (
        (r.type = 'subject_contains' AND :subject ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'body_contains'    AND :body    ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'sender_contains'  AND :sender  ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'sender_exact'     AND LOWER(:sender) = LOWER(r.pattern))
    )
  )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""


# Visibility filter mirrors ``MailAccountsRepo.list_account_ids_visible``:
# personal accounts (``ma.user_id = :user_id``) OR the caller's team
# accounts (``ma.group_id = :user_group_id``, only when the caller has a
# group). For super-admins / users without a group we pass NULL and the
# second branch evaluates to FALSE — the apply stays scoped to their own
# accounts, which matches existing super-admin UX (admins typically do not
# own team accounts).
# round-23: ``*_contains`` is whole-word, case-SENSITIVE (``~`` + ``\y``
# boundaries) over a regex-escaped pattern (literal match). The user
# controls case via the pattern they type. See module docstring.
APPLY_TAG_TO_EXISTING: Final[str] = r"""
INSERT INTO message_tags (message_id, tag_id)
SELECT m.id, :tag_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE (
        ma.user_id = :user_id
        OR (:user_group_id IS NOT NULL AND ma.group_id = :user_group_id)
    )
  AND EXISTS (
    SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id AND (
        (r.type = 'subject_contains' AND m.subject   ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'body_contains'    AND m.body_text ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'sender_contains'  AND m.from_addr ~ ('\y' || regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g') || '\y')) OR
        (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
    )
  )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""
