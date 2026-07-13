"""Runtime detector for deferred side effects dropped at session teardown.

Why (ADR-0046 §2.1/§2.1.1, `TD-054`): the mailbox status-channel hook in the
HTTP layer is **deferred** — the domain service does not push, it collects the
affected mailbox ids and the CALLER (the owner of the transaction: router,
worker job, CLI, wrapper) must call ``flush_crm_status_events()`` strictly AFTER
its COMMIT. A caller that forgets the flush loses the status event **silently**:
the pending list simply goes to GC with the service — no exception, no log, no
failing grep. For a deactivation the loss is unrecoverable (the mailbox drops
out of ``list_active()`` and never emits another status event).

This module makes that loss **noisy**, without repairing it:

- any component with post-COMMIT side effects registers a :class:`SessionGuard`
  against the session it works on (probe = "what is still un-flushed?");
- :func:`check_session_guards` runs at session teardown (``shared.db``:
  :func:`~shared.db.get_session` for the API, :func:`~shared.db.make_session`
  for the worker/CLI — the only two ways to obtain a session in this codebase,
  so *every* caller is covered, not just today's two routers);
- production: a structured ``warning`` per violated guard, and the request/cycle
  is NOT failed (an already-committed PATCH still returns 200);
- tests (strict mode): a hard ``AssertionError`` so a forgotten flush is caught
  on CI instead of on prod.

The guard deliberately **does NOT flush** the pending events itself: auto-flush
at teardown is rejected in ADR-0046 §Alternatives — teardown does not know
whether the transaction COMMITted or rolled back, so it could ship an event for
a rolled-back change.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final
from weakref import WeakKeyDictionary

from sqlalchemy.ext.asyncio import AsyncSession

from shared.logging import get_logger

log = get_logger(__name__)

#: Opt-in/opt-out override for the hard-fail behaviour. Unset → strict mode is
#: enabled only under pytest (``PYTEST_CURRENT_TEST``); production never fails.
STRICT_ENV_VAR: Final = "SESSION_GUARD_STRICT"

_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SessionGuard:
    """A post-COMMIT side effect that must be drained before the session dies.

    :param event: structured-log event name emitted when the probe is non-empty
        (e.g. ``crm_status_pending_dropped``).
    :param field: log/assert payload key for the leftover ids
        (e.g. ``mail_account_ids``).
    :param owner: component that registered the guard — for the log line.
    :param probe: returns the ids still pending. Empty → the caller did its job.

    .. warning::
        ``probe`` MUST NOT hold a strong reference to the session it is
        registered against (nor to any object that does — e.g. a domain service
        keeping ``self._db``). Guards are stored as the VALUES of a
        :class:`~weakref.WeakKeyDictionary` keyed by the session, and a value
        that strongly references its own key keeps that key alive forever: a
        session that never reaches the :mod:`shared.db` teardown (a direct
        ``AsyncSession(...)`` in a test or a future caller) would then pin
        itself, its identity map and the service for the life of the process.
        Close over the pending container itself (drained in place), not over the
        service — see ``MailAccountService.__init__``.
    """

    event: str
    field: str
    owner: str
    probe: Callable[[], Sequence[int]]


_registry: WeakKeyDictionary[AsyncSession, list[SessionGuard]] = WeakKeyDictionary()


def register_session_guard(session: AsyncSession, guard: SessionGuard) -> None:
    """Bind ``guard`` to ``session``; checked once, at the session's teardown.

    The registry is weak in the KEY *and* the guard must be weak in the VALUE
    (see :class:`SessionGuard`): a session that dies without passing through the
    :mod:`shared.db` teardown drops its guards with it instead of leaking.
    """
    _registry.setdefault(session, []).append(guard)


def strict_mode() -> bool:
    """Hard-fail on a violated guard? True under pytest, overridable by env."""
    raw = os.environ.get(STRICT_ENV_VAR)
    if raw is not None:
        return raw.strip().lower() in _TRUTHY
    return "PYTEST_CURRENT_TEST" in os.environ


def check_session_guards(session: AsyncSession, *, strict: bool | None = None) -> list[str]:
    """Detect un-drained post-COMMIT side effects on ``session``.

    Called from the session teardown in :mod:`shared.db`. Logs one ``warning``
    per violated guard and returns their rendered descriptions. In strict mode
    (tests) raises :class:`AssertionError` after logging.

    Never sends anything and never raises in production — an already-committed
    request must still return its 200 (ADR-0046 §2.1.1 / ``TD-054``).
    """
    guards = _registry.pop(session, [])
    violations: list[str] = []
    for guard in guards:
        leftover = list(guard.probe())
        if not leftover:
            continue
        log.warning(guard.event, owner=guard.owner, **{guard.field: leftover})
        violations.append(f"{guard.owner}.{guard.event}: {guard.field}={leftover}")
    if violations and (strict_mode() if strict is None else strict):
        raise AssertionError(
            "deferred side effects dropped at session teardown "
            "(a caller committed a status-writing change and never flushed it — "
            "ADR-0046 §2.1.1 / TD-054): " + "; ".join(violations)
        )
    return violations
