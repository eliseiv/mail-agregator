"""Cookie helpers — single place that knows the cookie names and attrs.

Cookie names: ``mas_session`` (HttpOnly, opaque token),
``mas_csrf`` (NOT HttpOnly, double-submit), ``mas_setup`` (HttpOnly,
short-lived setup-session for ``/set-password``), ``mas_login`` (HttpOnly,
short-lived state cookie carrying the username between step-1 and step-2 of
the two-step login flow — ADR-0016).

Per ``docs/06-security.md`` sec. 5: ``Secure`` flag only in prod (TLS is
terminated upstream by the nginx reverse proxy; in dev there is no TLS).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response

from shared.config import Settings

SESSION_COOKIE = "mas_session"
CSRF_COOKIE = "mas_csrf"
SETUP_COOKIE = "mas_setup"
LOGIN_COOKIE = "mas_login"

# Two-step login state cookie TTL — 15 minutes is enough for a normal user to
# move from the username form to the password form, and short enough that an
# abandoned session cannot be replayed later. Mirrors ``SETUP_SESSION_TTL_SECONDS``
# semantics but is intentionally separate so the two flows can diverge later.
LOGIN_COOKIE_MAX_AGE = 15 * 60


def set_session_cookies(
    response: Response, session_token: str, csrf: str, settings: Settings
) -> None:
    """Set ``mas_session`` (HttpOnly) and ``mas_csrf`` (readable by JS)."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )
    response.delete_cookie(
        key=CSRF_COOKIE,
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


def set_setup_cookie(response: Response, setup_token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SETUP_COOKIE,
        value=setup_token,
        max_age=settings.SETUP_SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


def clear_setup_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SETUP_COOKIE,
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


# ---------------------------------------------------------------------------
# Two-step login state cookie (ADR-0016)
# ---------------------------------------------------------------------------


def set_login_cookie(response: Response, username: str, settings: Settings) -> None:
    """Persist the username submitted at step-1 so step-2 can read it.

    The cookie value is the **plain** username (already normalised lower-case
    by the schema). It is HttpOnly so JavaScript on a (hypothetical) XSS-prone
    page cannot exfiltrate it; the username is not a secret per se but we
    avoid any risk of it being indexed by client-side analytics or forwarded
    to a third party. ``SameSite=Lax`` and ``Secure`` (prod) match the rest
    of the cookie family; the short ``Max-Age`` (15 min) bounds the window
    in which a stale user-agent could autofill the wrong account.
    """
    if not username:
        # Defensive — never set an empty cookie.
        return
    response.set_cookie(
        key=LOGIN_COOKIE,
        value=username,
        max_age=LOGIN_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


def clear_login_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=LOGIN_COOKIE,
        path="/",
        domain=settings.COOKIE_DOMAIN or None,
    )


def read_login_cookie(request: Request) -> str | None:
    """Return the lower-cased username carried over from step-1, if any."""
    raw = request.cookies.get(LOGIN_COOKIE)
    if not raw:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None
