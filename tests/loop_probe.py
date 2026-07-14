"""Event-loop liveness probe for the off-loop-resolve tests (ADR-0047 §4 / TD-056).

The defect being guarded against is invisible to ordinary assertions: a
BLOCKING ``socket.getaddrinfo`` called straight from a coroutine runs *in the
event-loop thread*, so the whole process stops scheduling — every other request,
every other mailbox in the sync cycle — and ``asyncio.wait_for`` (which can only
cancel at ``await`` points) never fires. Moving the call into
``asyncio.to_thread`` fixes it.

The only way to tell the two apart is to observe the LOOP: while the resolver
hangs, does anything else still get scheduled?

:func:`assert_loop_responsive_while` runs the call under test as a task, then
counts how many times it itself gets scheduled while the resolver is stuck. On a
blocked loop the counter cannot advance past ~1 (the very first ``await`` only
returns once the blocking call finishes); off-loop it advances freely.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from typing import Any

#: How long the fake resolver blocks. Long enough to be unmistakable, short
#: enough that the leaked worker thread dies right after the test.
BLOCK_SECONDS = 1.5

#: Ticks a healthy loop easily clears in ``BLOCK_SECONDS * 0.6`` at 20 ms each
#: (~45 in practice). A blocked loop yields exactly one. The gap is an order of
#: magnitude, so a slow CI runner cannot flip the verdict.
MIN_TICKS = 10


#: Only these hosts hang. ``socket.getaddrinfo`` is a PROCESS-WIDE global: redis,
#: postgres and the ASGI client resolve through it too, so a resolver that hangs
#: on EVERY name would take the whole test stack down with it (a 40 s redis
#: ``ConnectionError``, not the defect under test). The reserved documentation
#: domain (RFC 2606) is exactly the set of names our fixtures use for mail hosts.
HUNG_SUFFIX = ".example.com"


def hung_getaddrinfo(
    seconds: float = BLOCK_SECONDS, *, suffix: str = HUNG_SUFFIX
) -> Callable[..., list[Any]]:
    """A resolver that blocks the calling THREAD for ``suffix`` hosts only.

    Every other name (``localhost``, the docker services) is delegated to the
    REAL resolver captured at call time — so the fake is scoped to the external
    boundary under test and cannot stall the infrastructure of the test itself.
    """
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def _resolver(host: Any, port: Any = None, *args: Any, **kwargs: Any) -> list[Any]:
        if not (isinstance(host, str) and host.endswith(suffix)):
            return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[no-any-return]
        time.sleep(seconds)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", port or 993))]

    return _resolver


async def assert_loop_responsive_while(
    call: Callable[[], Coroutine[Any, Any, Any]],
    *,
    block_seconds: float = BLOCK_SECONDS,
    min_ticks: int = MIN_TICKS,
) -> None:
    """Run ``call`` (which will hang in the resolver) and prove the loop keeps running."""
    task: asyncio.Task[Any] = asyncio.create_task(call())
    # Let the task run up to its first suspension — i.e. into the resolver.
    await asyncio.sleep(0)

    ticks = 0
    until = time.monotonic() + block_seconds * 0.6
    while time.monotonic() < until:
        await asyncio.sleep(0.02)
        ticks += 1

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

    assert ticks >= min_ticks, (
        f"the event loop was scheduled only {ticks}x while getaddrinfo hung "
        f"({block_seconds}s) — the resolve is running IN the loop thread, not off it "
        "(ADR-0047 §4 / TD-056)"
    )
