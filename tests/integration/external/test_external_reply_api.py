"""Integration + contract tests for the external REPLY endpoint (ADR-0035).

``POST /api/external/messages/{id}/reply`` — the single WRITE endpoint of the
external API. A trusted B2B partner replies to an existing message with the
same ``X-API-Key`` used for the pull feed (ADR-0029). Narrow surface: no CRUD,
no arbitrary send, sender is NOT chosen (= the original message's mailbox),
scope = the canonical pull scope.

Source of truth: ``docs/adr/ADR-0035-external-reply-endpoint.md`` +
``docs/04-api-contracts.md`` §4d-reply +
``backend/app/external/{router,schemas}.py`` +
``backend/app/send/service.py`` (``send_external_reply`` / ``_send_core`` /
``_resolve_threading``).

Only the HTTP boundary is exercised through the network; DB state is seeded
directly against real Postgres so canonical-scope resolution, threading and the
``sent_messages`` persist run against actual SQL — never a mock of our own code.
The ONLY things mocked are the external SMTP transport (``aiosmtplib.send``) and
the best-effort IMAP "Sent" append (``_imap_append_blocking``) — i.e. the true
third-party boundaries (no real e-mail is ever sent).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, Message, SentMessage, User
from tests.integration.external.conftest import TEST_API_KEY

pytestmark = pytest.mark.integration


def _url(message_id: int | str) -> str:
    return f"/api/external/messages/{message_id}/reply"


# ===========================================================================
# Fixtures — reply feature gate, SMTP/IMAP stubs, seed helpers
# ===========================================================================


@pytest.fixture
def reply_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """Configure ``EXTERNAL_API_KEY`` + ``EXTERNAL_REPLY_ENABLED`` (+ optional
    reply rate cap) and reload the lru-cached settings.

    The router reads ``get_settings()`` fresh on every request (key, write-gate
    and the runtime rate ``Limit`` are all built per request), so setting the
    env vars + clearing the cache here makes the very next request observe them.
    Mirrors ``set_external_api_key`` in the pull conftest. Cache cleared again on
    teardown so later tests see the real ambient env.
    """

    def _set(
        *,
        key: str = TEST_API_KEY,
        enabled: bool = True,
        rate: int | None = None,
    ) -> Any:
        monkeypatch.setenv("EXTERNAL_API_KEY", key)
        monkeypatch.setenv("EXTERNAL_REPLY_ENABLED", "true" if enabled else "false")
        if rate is not None:
            monkeypatch.setenv("EXTERNAL_REPLY_RATE_LIMIT_PER_MINUTE", str(rate))
        get_settings.cache_clear()
        s = get_settings()
        assert s.EXTERNAL_API_KEY == key
        assert s.EXTERNAL_REPLY_ENABLED is enabled
        return s

    yield _set
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def reply_on(reply_env: Callable[..., Any]) -> str:
    """Turn the reply feature fully ON (valid key + write enabled). Returns key."""
    reply_env(key=TEST_API_KEY, enabled=True)
    return TEST_API_KEY


@pytest.fixture
def stub_smtp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch out the real SMTP transport + IMAP append; record what was sent.

    ``aiosmtplib.send`` (the password-path transport used by
    ``smtp_send_message``) and ``backend.app.send.service._imap_append_blocking``
    are the genuine third-party boundaries — mocking exactly these keeps the
    whole app pipeline (auth, gate, canonical resolve, MIME build, persist) real
    while never sending an actual e-mail.
    """
    import aiosmtplib

    from backend.app.send import service as svc_mod

    rec: dict[str, Any] = {"smtp_calls": 0, "msg": None, "recipients": None, "imap_calls": 0}

    async def _fake_send(*args: Any, **kwargs: Any) -> Any:
        rec["smtp_calls"] += 1
        rec["msg"] = args[0] if args else kwargs.get("message")
        rec["recipients"] = kwargs.get("recipients")
        rec["hostname"] = kwargs.get("hostname")
        rec["username"] = kwargs.get("username")
        return None

    def _fake_append(**kwargs: Any) -> None:
        rec["imap_calls"] += 1

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    monkeypatch.setattr(svc_mod, "_imap_append_blocking", _fake_append)
    return rec


async def _insert_message(
    db_engine: AsyncEngine,
    *,
    mail_account_id: int,
    uid: int = 1,
    from_addr: str = "sender@x.com",
    subject: str | None = "Hello",
    to_addrs: str = "me@example.com",
    message_id_header: str | None = None,
    refs_header: str | None = None,
) -> Message:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        m = Message(
            mail_account_id=mail_account_id,
            uid=uid,
            uidvalidity=1,
            from_addr=from_addr,
            from_name="Sender Name",
            to_addrs=to_addrs,
            cc_addrs=None,
            subject=subject,
            message_id_header=message_id_header,
            refs_header=refs_header,
            internal_date=datetime.now(UTC),
            body_text="original body",
            body_html=None,
            body_present=True,
            body_truncated=False,
        )
        ses.add(m)
        await ses.flush()
        await ses.refresh(m)
        return m


@pytest_asyncio.fixture
def make_original(db_engine: AsyncEngine) -> Callable[..., Any]:
    async def _make(mail_account_id: int, **kw: Any) -> Message:
        return await _insert_message(db_engine, mail_account_id=mail_account_id, **kw)

    return _make


async def _oauth_account(
    db_engine: AsyncEngine, *, user_id: int, email: str, needs_consent: bool
) -> MailAccount:
    """Insert an ``oauth_outlook`` account satisfying the DB CHECK constraints
    (``oauth_refresh_token_encrypted`` non-null + ``oauth_provider='outlook'``)."""
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        from backend.app.repositories.mail_accounts import MailAccountsRepo

        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=user_id,
            email=email,
            auth_type="oauth_outlook",
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=encrypt_mail_password("refresh", new_id),
            oauth_needs_consent=needs_consent,
            encrypted_password=None,
            imap_host="outlook.office365.com",
            imap_port=993,
            imap_ssl=True,
            smtp_host="smtp.office365.com",
            smtp_port=587,
            smtp_ssl=False,
            smtp_starttls=True,
        )
        ses.add(acc)
        await ses.flush()
        await ses.refresh(acc)
        return acc


async def _sent_rows(db_engine: AsyncEngine) -> list[SentMessage]:
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        return list((await ses.execute(select(SentMessage))).scalars().all())


# ===========================================================================
# 1. Positive path (ADR-0035 §2/§3/§5/§7)
# ===========================================================================


class TestReplyHappyPath:
    async def test_valid_reply_returns_200_subset_response_and_sends_via_mock(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        acc = await make_mail_account(super_admin.id, "canon@example.com")
        original = await make_original(
            acc.id,
            from_addr="alice@corp.example",
            subject="Order 42",
            message_id_header="<orig-42@corp.example>",
        )

        resp = await client.post(
            _url(original.id),
            headers={"X-API-Key": reply_on},
            json={"body": "Спасибо, приняли в работу."},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Response is the strict subset {sent_id, smtp_message_id} — NO
        # ``appended_to_sent`` in the external contract (ADR-0035 §5 / Q-0035-2).
        assert set(body.keys()) == {"sent_id", "smtp_message_id"}
        assert isinstance(body["sent_id"], int) and body["sent_id"] > 0
        assert body["smtp_message_id"].startswith("<") and body["smtp_message_id"].endswith(">")
        assert "appended_to_sent" not in body

        # Sent via the MOCK transport exactly once — never a real send.
        assert stub_smtp["smtp_calls"] == 1
        msg: EmailMessage = stub_smtp["msg"]
        # from = the original's mailbox (server-derived, never caller-chosen).
        assert msg["From"] == acc.email
        assert stub_smtp["hostname"] == acc.smtp_host
        # to defaults to [original.from_addr]; subject to "Re: " + original.subject.
        assert msg["To"] == "alice@corp.example"
        assert list(stub_smtp["recipients"]) == ["alice@corp.example"]
        assert msg["Subject"] == "Re: Order 42"
        # Threading built from the original (In-Reply-To / References).
        assert msg["In-Reply-To"] == "<orig-42@corp.example>"
        assert "<orig-42@corp.example>" in (msg["References"] or "")

        # ``sent_messages.user_id`` = the mailbox OWNER (ADR-0035 §7).
        rows = await _sent_rows(db_engine)
        assert len(rows) == 1
        assert rows[0].user_id == super_admin.id
        assert rows[0].from_account_id == acc.id
        assert rows[0].smtp_message_id == body["smtp_message_id"]

    async def test_subject_default_when_original_subject_null(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "canon2@example.com")
        original = await make_original(acc.id, subject=None, message_id_header="<n@x>")
        resp = await client.post(
            _url(original.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 200, resp.text
        # subject None -> "Re: " (ADR-0035 Edge cases).
        assert stub_smtp["msg"]["Subject"] == "Re: "

    async def test_explicit_to_and_cc_override_defaults(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "canon3@example.com")
        original = await make_original(acc.id, from_addr="alice@corp.example")
        resp = await client.post(
            _url(original.id),
            headers={"X-API-Key": reply_on},
            json={
                "to": ["ops@corp.example"],
                "cc": ["cc1@corp.example", "cc2@corp.example"],
                "body": "hi",
            },
        )
        assert resp.status_code == 200, resp.text
        msg = stub_smtp["msg"]
        assert msg["To"] == "ops@corp.example"
        assert set(stub_smtp["recipients"]) == {
            "ops@corp.example",
            "cc1@corp.example",
            "cc2@corp.example",
        }

    async def test_threading_absent_when_original_has_no_message_id(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "canon4@example.com")
        # No Message-ID header on the original -> nothing to thread onto.
        original = await make_original(acc.id, message_id_header=None)
        resp = await client.post(
            _url(original.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 200, resp.text
        msg = stub_smtp["msg"]
        assert msg["In-Reply-To"] is None
        assert msg["References"] is None


# ===========================================================================
# 2. Check ORDER (CRITICAL — ADR-0035 §3): 429 > 401 > 403 > 400
# ===========================================================================


class TestCheckOrder:
    _BAD_JSON = b"{ this is not valid json"  # would 400 if body validation ran

    async def test_wrong_key_preempts_body_validation_401_not_400(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # Feature ON, but the provided key is wrong AND the body is malformed.
        # Auth (step 2-4) runs BEFORE body parse (step 6) -> 401, never 400.
        reply_env(key=TEST_API_KEY, enabled=True)
        resp = await client.post(
            _url(1),
            headers={"X-API-Key": "wrong", "Content-Type": "application/json"},
            content=self._BAD_JSON,
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_preempts_write_gate_401_not_403(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # Write DISABLED and key WRONG. Auth (401) runs before the write-gate
        # (403) -> the opaque 401 wins (config/gate state never disclosed).
        reply_env(key=TEST_API_KEY, enabled=False)
        resp = await client.post(
            _url(1), headers={"X-API-Key": "wrong"}, json={"body": "hi"}
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_write_gate_preempts_body_validation_403_not_400(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # Valid key, write DISABLED, malformed body. Gate (step 5, 403) runs
        # BEFORE body parse (step 6, 400) -> 403.
        reply_env(key=TEST_API_KEY, enabled=False)
        resp = await client.post(
            _url(1),
            headers={"X-API-Key": TEST_API_KEY, "Content-Type": "application/json"},
            content=self._BAD_JSON,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_rate_limit_preempts_auth_429_not_401(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # Cap the reply budget to 1/min. Every request uses a WRONG key + a
        # malformed body — so if rate-limit did NOT run first they'd be 401/400.
        # The 2nd request must flip to 429 (rate-limit is step 1, before auth).
        reply_env(key=TEST_API_KEY, enabled=True, rate=1)
        ip = "203.0.113.201"  # TEST-NET-3, private to this test
        headers = {
            "X-API-Key": "wrong",
            "X-Forwarded-For": ip,
            "Content-Type": "application/json",
        }
        first = await client.post(_url(1), headers=headers, content=self._BAD_JSON)
        assert first.status_code == 401, first.text  # budget of 1 consumed, auth ran
        second = await client.post(_url(1), headers=headers, content=self._BAD_JSON)
        assert second.status_code == 429, second.text
        assert second.json()["error"]["code"] == "rate_limited"
        assert int(second.headers["retry-after"]) > 0

    async def test_rate_limit_preempts_write_gate_429_not_403(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # Cap 1/min, valid key, write DISABLED. 1st -> 403 (gate); 2nd -> 429
        # (rate-limit is consumed before the gate is even evaluated).
        reply_env(key=TEST_API_KEY, enabled=False, rate=1)
        ip = "203.0.113.202"
        headers = {"X-API-Key": TEST_API_KEY, "X-Forwarded-For": ip}
        first = await client.post(_url(1), headers=headers, json={"body": "hi"})
        assert first.status_code == 403, first.text
        second = await client.post(_url(1), headers=headers, json={"body": "hi"})
        assert second.status_code == 429, second.text
        assert int(second.headers["retry-after"]) > 0


# ===========================================================================
# 3. Auth + write-gate (ADR-0035 §1/§3/§6)
# ===========================================================================


class TestAuthAndGate:
    async def test_write_disabled_valid_key_returns_403(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        reply_env(key=TEST_API_KEY, enabled=False)
        resp = await client.post(
            _url(1), headers={"X-API-Key": TEST_API_KEY}, json={"body": "hi"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_no_key_returns_401(
        self, client: httpx.AsyncClient, reply_on: str
    ) -> None:
        resp = await client.post(_url(1), json={"body": "hi"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_returns_401(
        self, client: httpx.AsyncClient, reply_on: str
    ) -> None:
        resp = await client.post(_url(1), headers={"X-API-Key": "nope"}, json={"body": "hi"})
        assert resp.status_code == 401

    async def test_bearer_key_accepted(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "bearer@example.com")
        original = await make_original(acc.id, message_id_header="<b@x>")
        resp = await client.post(
            _url(original.id),
            headers={"Authorization": f"Bearer {reply_on}"},
            json={"body": "hi"},
        )
        assert resp.status_code == 200, resp.text

    async def test_feature_off_empty_key_returns_401(
        self, client: httpx.AsyncClient, reply_env: Callable[..., Any]
    ) -> None:
        # EXTERNAL_API_KEY empty => whole external API off; opaque 401 even
        # though EXTERNAL_REPLY_ENABLED would be true.
        reply_env(key="", enabled=True)
        resp = await client.post(
            _url(1), headers={"X-API-Key": TEST_API_KEY}, json={"body": "hi"}
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_reply_is_csrf_exempt(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        # No cookie session / no CSRF token, yet the POST is NOT rejected with
        # csrf_failed — the /api/external/ prefix is exempt (ADR-0035 §2). With
        # the write-gate off we still get the domain 403, never a 403 csrf.
        reply_env(key=TEST_API_KEY, enabled=False)
        resp = await client.post(
            _url(1), headers={"X-API-Key": TEST_API_KEY}, json={"body": "hi"}
        )
        assert resp.json()["error"]["code"] != "csrf_failed"


# ===========================================================================
# 4. Scope / 404 — existence not disclosed (ADR-0035 §3/§Edge cases)
# ===========================================================================


class TestScopeAndNotFound:
    async def test_nonexistent_id_returns_404(
        self, client: httpx.AsyncClient, reply_on: str, stub_smtp: dict[str, Any]
    ) -> None:
        resp = await client.post(
            _url(999_999), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        assert stub_smtp["smtp_calls"] == 0

    async def test_reply_on_non_canonical_duplicate_returns_404(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_secondary_team_mailbox: Callable[..., Any],
        make_original: Callable[..., Any],
        stub_smtp: dict[str, Any],
    ) -> None:
        # Two mailboxes, SAME lower(email). Canonical = MIN(id) (the admin's).
        # A message on the NON-canonical duplicate is never in the pull feed, so
        # replying to its id must 404 (existence outside scope not disclosed).
        acc_canon = await make_mail_account(super_admin.id, "Shared@Example.com")
        acc_dup = await make_secondary_team_mailbox(
            username="dup_owner", group_name="Dup Team", email="shared@example.com"
        )
        assert acc_canon.id < acc_dup.id
        dup_msg = await make_original(acc_dup.id, message_id_header="<dup@x>")
        resp = await client.post(
            _url(dup_msg.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        assert stub_smtp["smtp_calls"] == 0

    async def test_id_below_one_returns_404_not_pre_auth_400(
        self, client: httpx.AsyncClient, reply_on: str
    ) -> None:
        # ``{id}`` is a plain int path param; id=0 is a valid int route match
        # that resolves to "no such message" -> 404 (NOT a pre-auth 400), which
        # keeps the ADR-0035 §3 order intact.
        resp = await client.post(
            _url(0), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 404


# ===========================================================================
# 5. Body validation -> 400 (ADR-0035 §2/§6)
# ===========================================================================


class TestBodyValidation:
    @pytest_asyncio.fixture
    async def repliable(
        self,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
    ) -> int:
        """A real, in-scope message id so validation is what rejects (not 404)."""
        acc = await make_mail_account(super_admin.id, "valid@example.com")
        m = await make_original(acc.id, message_id_header="<v@x>")
        return m.id

    async def test_missing_body_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(_url(repliable), headers={"X-API-Key": reply_on}, json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_empty_body_returns_400_field_body(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable), headers={"X-API-Key": reply_on}, json={"body": "   "}
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["code"] == "validation_error"
        # ``details.errors[]`` points at the ``body`` field.
        locs = " ".join(e["loc"] for e in err["details"]["errors"])
        assert "body" in locs

    async def test_malformed_json_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on, "Content-Type": "application/json"},
            content=b"{ not json",
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_invalid_email_in_to_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on},
            json={"to": ["not-an-email"], "body": "hi"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "validation_error"

    async def test_invalid_email_in_cc_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on},
            json={"cc": ["bad@@x"], "body": "hi"},
        )
        assert resp.status_code == 400

    async def test_body_over_1_mib_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on},
            json={"body": "x" * (1_048_576 + 1)},
        )
        assert resp.status_code == 400

    async def test_subject_over_998_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on},
            json={"subject": "s" * 999, "body": "hi"},
        )
        assert resp.status_code == 400

    async def test_more_than_100_addresses_returns_400(
        self, client: httpx.AsyncClient, reply_on: str, repliable: int
    ) -> None:
        resp = await client.post(
            _url(repliable),
            headers={"X-API-Key": reply_on},
            json={"to": [f"u{i}@x.com" for i in range(101)], "body": "hi"},
        )
        assert resp.status_code == 400


# ===========================================================================
# 6. Fault tolerance (ADR-0035 §6 / Edge cases)
# ===========================================================================


class TestFaultTolerance:
    async def test_smtp_failure_returns_502_and_no_sent_row(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
        db_engine: AsyncEngine,
    ) -> None:
        import aiosmtplib

        async def _boom(*_a: Any, **_k: Any) -> None:
            raise aiosmtplib.SMTPConnectError("smtp down")

        monkeypatch.setattr(aiosmtplib, "send", _boom)

        acc = await make_mail_account(super_admin.id, "smtpfail@example.com")
        original = await make_original(acc.id, message_id_header="<s@x>")
        resp = await client.post(
            _url(original.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 502, resp.text
        assert resp.json()["error"]["code"] == "smtp_failed"
        # Send did NOT happen -> no sent_messages row persisted.
        assert await _sent_rows(db_engine) == []

    async def test_oauth_needs_consent_returns_409(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_original: Callable[..., Any],
        db_engine: AsyncEngine,
    ) -> None:
        acc = await _oauth_account(
            db_engine, user_id=super_admin.id, email="oauth@example.com", needs_consent=True
        )
        original = await make_original(acc.id, message_id_header="<o@x>")
        resp = await client.post(
            _url(original.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "oauth_reconsent_required"
        assert await _sent_rows(db_engine) == []

    async def test_imap_append_failure_still_returns_200(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
        db_engine: AsyncEngine,
    ) -> None:
        import aiosmtplib

        from backend.app.send import service as svc_mod

        async def _ok_send(*_a: Any, **_k: Any) -> None:
            return None

        def _bad_append(**_k: Any) -> None:
            raise OSError("imap connection refused")

        monkeypatch.setattr(aiosmtplib, "send", _ok_send)
        monkeypatch.setattr(svc_mod, "_imap_append_blocking", _bad_append)

        acc = await make_mail_account(super_admin.id, "imapfail@example.com")
        original = await make_original(acc.id, message_id_header="<i@x>")
        resp = await client.post(
            _url(original.id), headers={"X-API-Key": reply_on}, json={"body": "hi"}
        )
        # Best-effort append failed but the SEND succeeded -> 200 + persisted row.
        assert resp.status_code == 200, resp.text
        assert set(resp.json().keys()) == {"sent_id", "smtp_message_id"}
        rows = await _sent_rows(db_engine)
        assert len(rows) == 1


# ===========================================================================
# 7. Rate-limit — separate budget from the pull read limit (ADR-0035 §4)
# ===========================================================================


class TestRateLimitSeparateBudget:
    async def test_reply_budget_independent_from_pull_budget(
        self,
        client: httpx.AsyncClient,
        reply_env: Callable[..., Any],
    ) -> None:
        """Exhausting ``LIMIT_EXTERNAL_REPLY`` (30/min, here capped to 2) must
        NOT consume the read ``LIMIT_EXTERNAL_API`` budget: a pull GET from the
        SAME IP still succeeds after reply is throttled. The 429 also carries
        ``Retry-After`` (ADR-0035 §4/§6)."""
        reply_env(key=TEST_API_KEY, enabled=True, rate=2)
        ip = "203.0.113.210"
        # Wrong key on the reply so we exercise the rate-limit (step 1) without
        # needing a real message; auth is irrelevant to the counter.
        reply_headers = {"X-API-Key": "wrong", "X-Forwarded-For": ip}
        statuses = [
            (await client.post(_url(1), headers=reply_headers, json={"body": "x"})).status_code
            for _ in range(3)
        ]
        assert statuses[:2] == [401, 401]  # budget of 2 consumed (auth ran, wrong key)
        assert statuses[2] == 429
        throttled = await client.post(_url(1), headers=reply_headers, json={"body": "x"})
        assert throttled.status_code == 429
        assert int(throttled.headers["retry-after"]) > 0

        # The READ budget on the SAME IP is untouched (separate counter).
        pull = await client.get(
            "/api/external/messages",
            headers={"X-API-Key": TEST_API_KEY, "X-Forwarded-For": ip},
        )
        assert pull.status_code == 200, pull.text


# ===========================================================================
# 8. Regression — extraction of _send_core / _resolve_threading did not break
#    the pull GET nor coexistence in the same app (ADR-0035 §Migration step 6)
# ===========================================================================


class TestRegression:
    async def test_pull_get_still_works_alongside_reply(
        self,
        client: httpx.AsyncClient,
        reply_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_original: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "regress@example.com")
        original = await make_original(acc.id, message_id_header="<r@x>")
        pull = await client.get("/api/external/messages", headers={"X-API-Key": reply_on})
        assert pull.status_code == 200, pull.text
        ids = [m["id"] for m in pull.json()["messages"]]
        assert original.id in ids
