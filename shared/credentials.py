"""Normalisation of OPTIONAL credential strings (SMTP username / SMTP password).

Why this module exists (prod incident, 2026-07-15)
--------------------------------------------------
``mail_accounts.smtp_username`` is documented as *nullable, falls back to
``email``* (``docs/03-data-model.md``). In production 41 of 114 rows carried the
literal four-character text ``'None'`` instead of SQL ``NULL`` — the fingerprint
of an import that serialised a Python ``None`` through ``str()``. A non-empty
string is truthy, so the ``account.smtp_username or account.email`` fallback
selected ``'None'`` as the SMTP login and every send from those mailboxes died
with ``535 5.7.8 BadCredentials`` (IMAP, which logs in with ``email``, kept
working — hence the "sync is fine but nothing can be sent" signature).

The defect is in the CODE, not only in the data: an empty string, a
whitespace-only string and the sentinel texts ``'None'`` / ``'null'`` are all
the ABSENCE of a login, never a login. Normalising them here — at every point
where such a value is read or written — makes the failure unreproducible for any
future import or API caller, without touching the stored rows.

Two functions, deliberately different:

- :func:`normalize_optional_login` — for identifiers (SMTP username). Trimmed;
  surrounding whitespace in a login is always an artefact.
- :func:`normalize_optional_secret` — for secrets (SMTP password). The value is
  NEVER trimmed or otherwise rewritten: a password is opaque and may legitimately
  contain leading/trailing spaces. Only a *blank* value or a serialisation
  sentinel is treated as absence, which then falls back to the IMAP password
  exactly as a SQL ``NULL`` ``smtp_encrypted_password`` does.
"""

from __future__ import annotations

# Texts that can only be the result of serialising an absent value:
# Python ``str(None)``, JSON ``null`` / JS ``undefined``. They are never a real
# SMTP login and never a real password worth trying (an attempt with them is a
# guaranteed 535 anyway, which is why they must not shadow the documented
# fallback to ``email`` / ``encrypted_password``).
_ABSENCE_SENTINELS = frozenset({"none", "null", "undefined"})


def _is_absent(value: str) -> bool:
    stripped = value.strip()
    return not stripped or stripped.lower() in _ABSENCE_SENTINELS


def normalize_optional_login(value: str | None) -> str | None:
    """Return a usable SMTP login, or ``None`` when the value means "no login".

    ``None`` / ``""`` / ``"   "`` / ``"None"`` / ``"null"`` (any case) → ``None``.
    Anything else is returned trimmed.
    """
    if value is None:
        return None
    if _is_absent(value):
        return None
    return value.strip()


def normalize_optional_secret(value: str | None) -> str | None:
    """Return a usable secret, or ``None`` when the value means "no secret".

    Same absence rules as :func:`normalize_optional_login`, but a surviving value
    is returned **verbatim** — secrets are never trimmed or rewritten.
    """
    if value is None:
        return None
    if _is_absent(value):
        return None
    return value
