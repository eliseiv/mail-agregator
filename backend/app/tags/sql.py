r"""Parameterised SQL used by the tags module (ADR-0017 §5/§7).

Two queries (both honour the round-10 team-visibility model — see
``docs/05-modules.md`` sec. 17 + ADR-0019 / ADR-0022 production patch):

* :data:`APPLY_TAGS_TO_MESSAGE` — given a single freshly-inserted
  ``message_id`` plus its ``mail_account_id`` and resolved subject / body
  / sender, INSERT one row in ``message_tags`` for every matching tag of
  every user who can SEE that message. A user sees a message when either
  (a) they own its mail account, or (b) they are a member (via
  ``user_groups`` — home or additional, ADR-0030) of the mail account's
  team. This is the worker hook called after ``insert_message_idempotent``
  in ``worker.app.sync_cycle``.

* :data:`APPLY_TAG_TO_EXISTING` — bulk INSERT every existing message
  visible to the tag's owner that matches the given ``tag_id``'s rules.
  Visibility (ADR-0030) = personal accounts (``ma.user_id = :user_id``)
  plus accounts of any team the owner is a member of via ``user_groups``
  (``EXISTS (… ug.user_id = :user_id AND ug.group_id = ma.group_id)``). A
  user without any membership matches only personal accounts (the EXISTS is
  FALSE). round-26: a super-admin (``:is_super_admin = TRUE``)
  applies to EVERY message in the system — the flag forces the visibility
  filter to TRUE for all rows, matching the super-admin read scope
  (``MessageService.visible_user_ids`` → None). Called from
  ``POST /api/tags`` (when ``apply_to_existing=true``) and
  ``POST /api/tags/{id}/apply-to-existing``.

Both queries are idempotent (``ON CONFLICT (message_id, tag_id) DO NOTHING``).

whole-word, case-sensitive, normalised matching (ADR-0017 §4/§4.1/§4.2):
the three ``*_contains`` rule types (``subject_contains`` /
``body_contains`` / ``sender_contains``) match on **whole words,
case-SENSITIVELY**, over **whitespace-normalised** text, using the POSIX
case-sensitive regex operator ``~`` (``~*`` is NOT used). Each predicate
arm has the canonical form::

    norm(value) ~ ('(^|[^[:alnum:]_])' || norm(escaped_pattern) || '([^[:alnum:]_]|$)')

where::

    escaped_pattern = regexp_replace(pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g')
    norm(x)         = regexp_replace(translate(x, chr(160), ' '), '\s+', ' ', 'g')

**Word boundaries — explicit boundary classes, NOT ``\y`` (round-27 fix,
ADR-0017 §4.1).** The boundary is "start-of-string **or** a
non-alphanumeric-and-non-``_`` char" on the left and "a
non-alphanumeric-and-non-``_`` char **or** end-of-string" on the right. An
earlier revision (round-23) wrapped the pattern in ``\y … \y``
(word-boundary). That was a bug: ``\y`` is the transition between a *word*
and a *non-word* char, so a pattern that **begins or ends with
punctuation** never matched — after a trailing ``.``/``!`` comes
whitespace/end-of-string, both sides non-word → no boundary → no match.
This broke real user tags such as ``body_contains = "We noticed an issue
… requires your attention."`` (trailing dot) and ``"Congratulations!"``
(trailing ``!``); under ``match_mode='all'`` a single non-matching rule
drops the whole tag. The explicit boundary classes keep the exact same
whole-word guarantee for alphanumeric patterns (``PLA`` inside ``DPLA``
does not match as a word; ``pla`` ≠ ``PLA``) **and** work correctly for
punctuation-bounded patterns — the (escaped) punctuation char of the
pattern is itself non-word, while the boundary class inspects the
neighbour *outside* the pattern.

**Whitespace normalisation — MANDATORY (round-27, ADR-0017 §4.2).** Real
``messages.body_text`` (built from ``text/plain`` or ``html2text(html)``
in ``worker/app/imap_fetcher.py``) carries hard line breaks, runs of 2+
spaces (table/wrapper artefacts) and non-breaking spaces U+00A0 *inside*
one logical sentence. Without normalisation, multi-word patterns silently
fail to match. ``norm(x)`` first ``translate``s U+00A0 (``chr(160)``) to a
regular space, **then** collapses any whitespace run to a single space.
Order matters: in this deployment's locale Postgres ``\s`` /
``[[:space:]]`` do **not** treat U+00A0 as whitespace, so nbsp must be
translated explicitly **before** the ``\s+`` collapse. ``norm()`` is
applied to **both** sides of the comparison — to the value
(``subject`` / ``body_text`` / ``from_addr`` / ``COALESCE(from_name,'')``
and the corresponding binds) **and** to the already-escaped pattern
(``\\`` is not whitespace, so applying ``norm()`` after escaping is safe —
no conflict). Zero-width chars (U+200B/U+FEFF) are stripped upstream in
``strip_invisible_padding`` and never reach here.

Case-sensitivity is deliberate: the **user controls the case** by what they
type into the pattern. If a rule pattern is ``DPLA`` (caps), only an exact
capitalised whole-word ``DPLA`` in the text matches — e.g. ``Program
Licence Agreement ("DPLA")`` matches, but a lowercase ``dpla`` does not.
This gives a double safeguard against false positives: wrong case (e.g.
``pla`` ≠ ``PLA``) *and* substring-inside-a-word (boundary classes) are
both rejected.

The user pattern is escaped with ``regexp_replace`` so every regex
metacharacter is treated literally (e.g. the ``.`` in ``DPLA.PLA`` matches a
literal dot, not "any char"). ``sender_exact`` is unchanged
(``LOWER(...) = LOWER(...)`` — email/domain matching is de-facto
case-insensitive and the address is a single token without internal
whitespace, so it gets neither ``norm()`` nor boundary classes).

This is bounded-linear in practice (anchored literal alternations over a
fixed pattern, plus our own fixed boundary classes and ``\s+``) — no
user-supplied regex structure reaches the engine because every
metacharacter is escaped first, so the ReDoS argument of ADR-0017 §4 /
Alternatives A2 still holds. See ADR-0017 §4 and
``docs/100-known-tech-debt.md`` (round-23 / TD-022).

round-24 (per-tag match mode — migration 20260521_015): each tag now
carries ``tags.match_mode`` ∈ {``'any'``, ``'all'``}. ``'any'`` (the
default, backward-compatible) keeps the original OR semantics — the tag
attaches when *any* one rule matches. ``'all'`` requires the tag to have
at least one rule and that *every* rule match; we express AND as "the tag
has >=1 rule AND no rule fails to match" (``EXISTS(rule)`` +
``NOT EXISTS(rule WHERE NOT predicate)``). The ``<predicate(r)>`` block is
intentionally duplicated between the ``EXISTS`` (any) and ``NOT EXISTS``
(all) branches — SQL has no easy way to factor it out of two correlated
subqueries, and the duplication keeps the whole-word escaping identical in
both. In :data:`APPLY_TAGS_TO_MESSAGE` the mode is read from the ``t``
alias (``t.match_mode``); in :data:`APPLY_TAG_TO_EXISTING` there is no tag
alias in scope, so it is read via the scalar subquery
``(SELECT match_mode FROM tags WHERE id = :tag_id)``.

round-25 (sender_contains matches sender display-name too): the
``sender_contains`` rule type now matches when the (whole-word,
case-sensitive) pattern is found in **either** the sender email
(``from_addr`` / ``:sender``) **or** the sender display-name
(``from_name`` / ``:sender_name``). Apple's App Store Connect mail arrives
from a generic address such as ``no_reply@email.apple.com`` while the
display-name is the meaningful ``App Store Connect`` — a
``sender_contains: App Store Connect`` rule would never fire against the
address alone. The name side is wrapped in ``COALESCE(..., '')`` because
``from_name`` is nullable (a NULL ``~`` always yields NULL → the row is
dropped, so we coalesce to an empty string which simply never matches).
``sender_exact`` is deliberately left as an email-only exact match — it
exists for precise address routing (e.g. ``AppStoreNotices@apple.com``)
where the display-name is irrelevant. The same whole-word escaping applies
to the name side; both branches in each of the ``any``/``all`` predicates
carry the additional name comparison.

round-29 (``body_contains`` matches ``body_text`` AND text from ``body_html``;
ADR-0017 §4.3): a message is stored in two bodies — ``body_text`` (the
``text/plain`` part, or ``html2text(html)`` when no plain part) and
``body_html`` (the raw ``text/html`` part as received). **The UI renders
``body_html``**, so the user reads the HTML version with their eyes. Apple's
MIME mail carries **different text** in the two parts: a "reject" mail had
``body_text`` = "During our review, we noticed an issue with your
submission." (does NOT contain the «Реджект» pattern) while ``body_html``
= "We noticed an issue with your submission that requires your attention."
(DOES contain it). Pre-round-29 ``body_contains`` matched ``body_text``
only, so the tag never attached to a mail in which the user plainly *sees*
the trigger phrase. Fix: the ``body_contains`` arm now matches if the
pattern is found in ``body_text`` **OR** in the tag-stripped ``body_html``::

    norm(body_text)                            ~ boundary(norm(escaped_pattern))
    OR norm(strip_tags(COALESCE(body_html,''))) ~ boundary(norm(escaped_pattern))

where ``strip_tags(x) = regexp_replace(x, '<[^>]+>', ' ', 'g')`` (each HTML
tag → a space, applied **before** ``norm()`` so the runs of spaces it
creates at tag seams get collapsed — otherwise a multi-word pattern would
not match across the ``</p><p>`` boundary). ``body_html`` is wrapped in
``COALESCE(…, '')`` because the column is nullable (NULL ``~`` → NULL → the
row drops; an empty string simply never matches). **Only ``body_contains``
is affected** — ``subject_contains`` stays on ``subject``, ``sender_*`` on
``from_addr``/``from_name`` (HTML lives only in the body). In
:data:`APPLY_TAGS_TO_MESSAGE` the HTML side is a new bind
``COALESCE(CAST(:body_html AS TEXT), '')`` (CAST against
``AmbiguousParameterError``, same reason as ``:sender_name``); in
:data:`APPLY_TAG_TO_EXISTING` it reads the ``m.body_html`` column directly
(no bind). **Limitation (TD-024):** ``strip_tags`` removes only ``<…>``
tags, it does **not** decode HTML entities (``&amp;``/``&#39;``/``&nbsp;``),
so a pattern matching a phrase that contains entities is missed on the HTML
side. The current Apple phrase is entity-free, so the fix works; the general
case is tracked as TD-024.
"""

from __future__ import annotations

from typing import Final

# NB: parameters are positional named-binds via SQLAlchemy ``text(...)``;
# ``sender`` is used twice in the ``sender_exact`` branch so we render it
# once and let SQLAlchemy reuse the bind on both sides of the comparison.
#
# Visibility join: a tag belongs to user ``t.user_id``; that user sees the
# new message iff either they own the mail account OR (ma.group_id IS NOT
# NULL AND the user is a member of ma's team via ``user_groups`` — ADR-0030,
# home + additional) OR they are a super_admin (round-28).
# The membership EXISTS replaces the pre-ADR-0030 single-group predicate
# ``u.group_id = ma.group_id`` so a member of several teams is auto-tagged
# (and thus notified) on every team's mail. Team accounts retain their
# original ``group_id`` even when the owner is moved to a different group
# (round-10 production patch). round-28 (ADR-0017 §5.1) adds ``OR u.role =
# 'super_admin'`` so a super_admin's personal tags attach to EVERY message
# in the system → their TG-notifications fire (recipient SQL already has a
# super_admin branch). This is symmetric to round-26 in APPLY_TAG_TO_EXISTING.
# The webhook channel stays isolated from these tags (see ADR-0023 §3.2).
# round-27: ``*_contains`` is whole-word, case-SENSITIVE (``~`` + explicit
# boundary classes) over a whitespace-normalised, regex-escaped pattern
# (literal match). The user controls case via the pattern they type. See
# module docstring.
APPLY_TAGS_TO_MESSAGE: Final[str] = r"""
INSERT INTO message_tags (message_id, tag_id)
SELECT :message_id, t.id
FROM tags t
LEFT JOIN users u ON u.id = t.user_id
JOIN mail_accounts ma ON ma.id = :mail_account_id
WHERE (
        -- ADR-0040: a global tag (t.user_id IS NULL) applies to EVERY message
        -- of the system. The join to ``users`` is LEFT so a global tag (which
        -- has no owner row) is not dropped by an INNER JOIN; the visibility
        -- predicate below short-circuits on this branch (u is NULL for globals).
        t.user_id IS NULL
        OR u.id = ma.user_id
        OR (ma.group_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM user_groups ug
                WHERE  ug.user_id = u.id
                  AND  ug.group_id = ma.group_id
            ))
        OR u.role = 'super_admin'
    )
  AND (
        -- match_mode = 'any' (OR, default): at least one rule of the tag matches.
        (t.match_mode = 'any' AND EXISTS (
            SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id AND (
                (r.type = 'subject_contains' AND regexp_replace(translate(:subject, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')) OR
                (r.type = 'body_contains'    AND (
                    regexp_replace(translate(:body, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    OR regexp_replace(translate(regexp_replace(COALESCE(CAST(:body_html AS TEXT), ''), '<[^>]+>', ' ', 'g'), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                )) OR
                (r.type = 'sender_contains'  AND (
                    regexp_replace(translate(:sender, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    OR regexp_replace(translate(COALESCE(CAST(:sender_name AS TEXT), ''), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                )) OR
                (r.type = 'sender_exact'     AND LOWER(:sender) = LOWER(r.pattern))
            )
        ))
        OR
        -- match_mode = 'all' (AND): the tag has >=1 rule AND no rule fails to
        -- match (i.e. there is no rule for which the predicate is false).
        (t.match_mode = 'all'
            AND EXISTS (SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id)
            AND NOT EXISTS (
                SELECT 1 FROM tag_rules r WHERE r.tag_id = t.id AND NOT (
                    (r.type = 'subject_contains' AND regexp_replace(translate(:subject, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')) OR
                    (r.type = 'body_contains'    AND (
                        regexp_replace(translate(:body, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                        OR regexp_replace(translate(regexp_replace(COALESCE(CAST(:body_html AS TEXT), ''), '<[^>]+>', ' ', 'g'), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    )) OR
                    (r.type = 'sender_contains'  AND (
                        regexp_replace(translate(:sender, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                        OR regexp_replace(translate(COALESCE(CAST(:sender_name AS TEXT), ''), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    )) OR
                    (r.type = 'sender_exact'     AND LOWER(:sender) = LOWER(r.pattern))
                )
            )
        )
    )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""


# Visibility filter mirrors ``MailAccountsRepo.list_account_ids_visible``
# (ADR-0030 multi-group): personal accounts (``ma.user_id = :user_id``) OR
# accounts of any team the owner is a member of via ``user_groups`` (EXISTS
# keyed on ``:user_id``). round-26: a super-admin passes ``:is_super_admin =
# TRUE`` which
# forces the whole filter to TRUE so the apply reaches EVERY message in the
# system — matching the super-admin read scope (super-admin sees all
# messages via ``MessageService.visible_user_ids`` → None). For
# group_leader / group_member the flag is FALSE and the original
# personal+team scoping applies unchanged.
# round-27: ``*_contains`` is whole-word, case-SENSITIVE (``~`` + explicit
# boundary classes) over a whitespace-normalised, regex-escaped pattern
# (literal match). The user controls case via the pattern they type. See
# module docstring.
APPLY_TAG_TO_EXISTING: Final[str] = r"""
INSERT INTO message_tags (message_id, tag_id)
SELECT m.id, :tag_id
FROM messages m
JOIN mail_accounts ma ON ma.id = m.mail_account_id
WHERE (
        -- round-26 (super-admin full reach): a super-admin applies the tag to
        -- EVERY message in the system, not just their own/team accounts. The
        -- ``:is_super_admin`` flag short-circuits the visibility filter to TRUE
        -- for all rows. CAST is required: asyncpg cannot infer a
        -- prepared-statement type for a bare parameter, so we pin it to
        -- BOOLEAN to avoid AmbiguousParameterError.
        CAST(:is_super_admin AS BOOLEAN)
        OR ma.user_id = :user_id
        -- ADR-0030 (multi-group): the tag owner sees a team account when they
        -- are a member of that account's team — via ``user_groups`` (home +
        -- additional), not only their single home ``users.group_id``. The
        -- EXISTS naturally handles the no-group case (no membership rows →
        -- FALSE) and needs no NULL/CAST gymnastics.
        OR (ma.group_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM user_groups ug
                WHERE  ug.user_id = :user_id
                  AND  ug.group_id = ma.group_id
            ))
    )
  AND (
        -- match_mode = 'any' (OR, default): at least one rule of the tag matches.
        ((SELECT match_mode FROM tags WHERE id = :tag_id) = 'any' AND EXISTS (
            SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id AND (
                (r.type = 'subject_contains' AND regexp_replace(translate(m.subject,   chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')) OR
                (r.type = 'body_contains'    AND (
                    regexp_replace(translate(m.body_text, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    OR regexp_replace(translate(regexp_replace(COALESCE(m.body_html, ''), '<[^>]+>', ' ', 'g'), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                )) OR
                (r.type = 'sender_contains'  AND (
                    regexp_replace(translate(m.from_addr, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    OR regexp_replace(translate(COALESCE(m.from_name, ''), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                )) OR
                (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
            )
        ))
        OR
        -- match_mode = 'all' (AND): the tag has >=1 rule AND no rule fails to match.
        ((SELECT match_mode FROM tags WHERE id = :tag_id) = 'all'
            AND EXISTS (SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id)
            AND NOT EXISTS (
                SELECT 1 FROM tag_rules r WHERE r.tag_id = :tag_id AND NOT (
                    (r.type = 'subject_contains' AND regexp_replace(translate(m.subject,   chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')) OR
                    (r.type = 'body_contains'    AND (
                        regexp_replace(translate(m.body_text, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                        OR regexp_replace(translate(regexp_replace(COALESCE(m.body_html, ''), '<[^>]+>', ' ', 'g'), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    )) OR
                    (r.type = 'sender_contains'  AND (
                        regexp_replace(translate(m.from_addr, chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                        OR regexp_replace(translate(COALESCE(m.from_name, ''), chr(160), ' '), '\s+', ' ', 'g') ~ ('(^|[^[:alnum:]_])' || regexp_replace(translate(regexp_replace(r.pattern, '([\^$.|?*+()\[\]{}\\])', '\\\1', 'g'), chr(160), ' '), '\s+', ' ', 'g') || '([^[:alnum:]_]|$)')
                    )) OR
                    (r.type = 'sender_exact'     AND LOWER(m.from_addr) = LOWER(r.pattern))
                )
            )
        )
    )
ON CONFLICT (message_id, tag_id) DO NOTHING
"""
