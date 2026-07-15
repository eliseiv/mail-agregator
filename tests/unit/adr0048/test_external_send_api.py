"""Contract + integration tests for the GENERIC SEND endpoint (ADR-0048 §1, phase A2.1).

``POST /api/external/mailboxes/{id}/send`` — the endpoint the CRM calls to answer a
message. The CRM owns the message store, the reply defaults and the threading
headers; the aggregator is a thin SMTP executor. Its absence was a **live production
bug** (the CRM got a 404 for four days — TD-059), so this suite is deliberately in the
CI-gated lane (see ``conftest.py``).

Source of truth: ``docs/adr/ADR-0048-external-send-contract-and-reply-restore.md``
§1/§2/§4 + ``docs/04-api-contracts.md`` §4f-send + ``backend/app/external/{router,schemas}.py``
+ ``backend/app/send/service.py`` (``send_from_mailbox`` / ``_send_transport``).

What is real and what is mocked: the whole app pipeline (rate-limit → key → write-gate
→ body validation → mailbox resolve → MIME build → transport) runs for real against real
Postgres/Redis. The ONLY mocks are the genuine third-party boundaries — the SMTP
transport (``aiosmtplib.send``) and the best-effort IMAP "Sent" append
(``_imap_append_blocking``). No e-mail is ever sent.

Headline case (``TestFoldedThreadingHeaders``): a REAL folded ``References`` header from
a long thread (``'<a@x.com>\\r\\n <b@x.com>\\r\\n\\t<c@x.com>'``) must go through — every
Message-ID preserved, header unfolded, no 400/500. Without it, answering a message in a
long conversation breaks again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.config import get_settings
from shared.crypto import encrypt_mail_password
from shared.models import MailAccount, User

pytestmark = pytest.mark.integration

TEST_API_KEY = "test_external_api_key_deadbeefdeadbeefdeadbeefdeadbeef"

# ``localhost`` (not example.com): the SSRF guard resolves the host for real
# (``assert_public_host_async``) and private targets are allowed in ``APP_ENV=dev``
# (the CI test env) — so the resolve leg stays deterministic and offline.
_MAIL_HOST = "localhost"


def _url(account_id: int | str) -> str:
    return f"/api/external/mailboxes/{account_id}/send"


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"to": ["dest@example.com"], "body_text": "Hello there"}
    base.update(over)
    return base


# ===========================================================================
# Fixtures — feature gates, SMTP/IMAP stubs, mailbox seed
# ===========================================================================


@pytest.fixture
def external_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """Set the external-API env (key / write-gate / limits) and reload the settings.

    The router reads ``get_settings()`` fresh on every request (key, gate and the
    runtime rate ``Limit`` are all built per request), so setting the env vars +
    clearing the ``lru_cache`` makes the very next request observe them.
    """

    def _set(
        *,
        key: str = TEST_API_KEY,
        write_enabled: bool = True,
        write_rate: int | None = None,
        reply_enabled: bool | None = None,
    ) -> Any:
        monkeypatch.setenv("EXTERNAL_API_KEY", key)
        monkeypatch.setenv("EXTERNAL_WRITE_ENABLED", "true" if write_enabled else "false")
        if write_rate is not None:
            monkeypatch.setenv("EXTERNAL_WRITE_RATE_LIMIT_PER_MINUTE", str(write_rate))
        if reply_enabled is not None:
            monkeypatch.setenv("EXTERNAL_REPLY_ENABLED", "true" if reply_enabled else "false")
        get_settings.cache_clear()
        s = get_settings()
        assert key == s.EXTERNAL_API_KEY
        assert s.EXTERNAL_WRITE_ENABLED is write_enabled
        return s

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def write_on(external_env: Callable[..., Any]) -> str:
    """Whole write surface ON: valid key + ``EXTERNAL_WRITE_ENABLED=true``."""
    external_env(key=TEST_API_KEY, write_enabled=True)
    return TEST_API_KEY


@pytest.fixture
def stub_smtp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch out the real SMTP transport + the IMAP append; record what was sent.

    ``aiosmtplib.send`` (the password-path transport used by ``smtp_send_message``)
    and ``_imap_append_blocking`` are the genuine third-party boundaries — mocking
    exactly these keeps the whole app pipeline real while never sending e-mail.
    """
    import aiosmtplib

    from backend.app.send import service as svc_mod

    rec: dict[str, Any] = {"smtp_calls": 0, "msg": None, "recipients": None, "imap_calls": 0}

    async def _fake_send(*args: Any, **kwargs: Any) -> None:
        rec["smtp_calls"] += 1
        rec["msg"] = args[0] if args else kwargs.get("message")
        rec["recipients"] = kwargs.get("recipients")
        rec["hostname"] = kwargs.get("hostname")
        rec["username"] = kwargs.get("username")

    def _fake_append(**_kwargs: Any) -> None:
        rec["imap_calls"] += 1

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    monkeypatch.setattr(svc_mod, "_imap_append_blocking", _fake_append)
    return rec


async def _crm_service_user(db_engine: AsyncEngine) -> User:
    from backend.app.auth.service import CRM_SERVICE_USERNAME

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        u = (
            await ses.execute(select(User).where(User.username == CRM_SERVICE_USERNAME))
        ).scalar_one_or_none()
    assert u is not None, "crm-service must be seeded by app startup (seed_crm_service_user)"
    return u


@pytest_asyncio.fixture
async def mailbox(app: Any, db_engine: AsyncEngine) -> AsyncIterator[MailAccount]:
    """A password mailbox owned by ``crm-service`` (the headless owner, ADR-0039).

    Depends on ``app`` so the lifespan seed has already run (the autouse TRUNCATE
    wipes ``users`` before each test).
    """
    owner = await _crm_service_user(db_engine)
    from backend.app.repositories.mail_accounts import MailAccountsRepo

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses, ses.begin():
        new_id = await MailAccountsRepo(ses).next_account_id()
        acc = MailAccount(
            id=new_id,
            user_id=owner.id,
            email="box@example.com",
            display_name="Box",
            encrypted_password=encrypt_mail_password("p", new_id),
            imap_host=_MAIL_HOST,
            imap_port=993,
            imap_ssl=True,
            smtp_host=_MAIL_HOST,
            smtp_port=465,
            smtp_ssl=True,
            smtp_starttls=False,
        )
        ses.add(acc)
        await ses.flush()
        await ses.refresh(acc)
    yield acc


async def _sent_messages_count(db_engine: AsyncEngine) -> int:
    # The ``sent_messages`` table still exists (its DROP TABLE is the later DDL
    # phase D, ADR-0048 §3) but the ORM class was removed with the reply writer,
    # so this regression gate counts rows via raw SQL, not the mapped class.
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with factory() as ses:
        n = (await ses.execute(text("SELECT count(*) FROM sent_messages"))).scalar_one()
    return int(n)


# ===========================================================================
# 1. Happy path + response contract (ADR-0048 §1)
# ===========================================================================


class TestSendContract:
    async def test_200_returns_exactly_smtp_message_id(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # EXACT shape — ``sent_id`` is deliberately absent (ADR-0048 §1: the
        # aggregator keeps no durable record, so any id would point at no row).
        assert set(payload.keys()) == {"smtp_message_id"}
        assert isinstance(payload["smtp_message_id"], str)
        assert payload["smtp_message_id"].startswith("<")
        assert stub_smtp["smtp_calls"] == 1

    async def test_message_id_on_the_wire_matches_the_response(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 200
        msg: EmailMessage = stub_smtp["msg"]
        assert msg["Message-ID"] == resp.json()["smtp_message_id"]

    async def test_sender_is_the_path_mailbox_recipients_are_to_plus_cc(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(
            _url(mailbox.id),
            json=_body(to=["a@x.com"], cc=["c@x.com"], subject="Re: hi"),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200
        msg: EmailMessage = stub_smtp["msg"]
        assert msg["From"] == mailbox.email  # sender = mailbox {id}, never a body field
        assert msg["To"] == "a@x.com"
        assert msg["Cc"] == "c@x.com"
        assert msg["Subject"] == "Re: hi"
        assert stub_smtp["recipients"] == ["a@x.com", "c@x.com"]

    async def test_bearer_key_is_accepted_too(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(
            _url(mailbox.id),
            json=_body(),
            headers={"Authorization": f"Bearer {write_on}"},
        )
        assert resp.status_code == 200

    async def test_send_writes_no_sent_messages_row(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
        db_engine: AsyncEngine,
    ) -> None:
        """REGRESSION GATE (ADR-0048 §1/§3 A2.1): the generic send persists NOTHING.

        The durable log of what was sent lives in the CRM (``mail_sent_messages``);
        ``sent_messages`` is under drop (ADR-0044 §1) and its last writer must not be
        this path — otherwise the table can never be dropped.
        """
        before = await _sent_messages_count(db_engine)

        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 200

        after = await _sent_messages_count(db_engine)
        assert after == before, "the generic send must not write a sent_messages row"


# ===========================================================================
# 2. Gate + ORDER of the checks (rate-limit → key → write-gate → body)
# ===========================================================================


class TestAuthGateOrder:
    async def test_no_key_401(
        self, client: httpx.AsyncClient, write_on: str, mailbox: MailAccount
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=_body())
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "not_authenticated"

    async def test_wrong_key_401(
        self, client: httpx.AsyncClient, write_on: str, mailbox: MailAccount
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    async def test_feature_off_empty_key_401_unenumerable(
        self,
        client: httpx.AsyncClient,
        external_env: Callable[..., Any],
        mailbox: MailAccount,
    ) -> None:
        # ``EXTERNAL_API_KEY=""`` turns the whole external API off — even an empty
        # provided key must never "accidentally match".
        external_env(key="", write_enabled=True)
        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": ""})
        assert resp.status_code == 401

    async def test_valid_key_but_write_gate_off_403(
        self,
        client: httpx.AsyncClient,
        external_env: Callable[..., Any],
        mailbox: MailAccount,
    ) -> None:
        external_env(key=TEST_API_KEY, write_enabled=False)
        resp = await client.post(
            _url(mailbox.id), json=_body(), headers={"X-API-Key": TEST_API_KEY}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    async def test_broken_body_without_key_is_401_not_400(
        self, client: httpx.AsyncClient, write_on: str, mailbox: MailAccount
    ) -> None:
        """ORDER: the key is checked BEFORE the body is parsed.

        A garbage body from an unauthenticated caller must never reveal validation
        detail (nor cost a body parse) — it is a plain 401.
        """
        resp = await client.post(_url(mailbox.id), json={"nonsense": True})
        assert resp.status_code == 401

    async def test_broken_body_with_write_gate_off_is_403_not_400(
        self,
        client: httpx.AsyncClient,
        external_env: Callable[..., Any],
        mailbox: MailAccount,
    ) -> None:
        """ORDER: the write-gate is checked BEFORE the body is parsed."""
        external_env(key=TEST_API_KEY, write_enabled=False)
        resp = await client.post(
            _url(mailbox.id),
            json={"nonsense": True},
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 403

    async def test_rate_limit_429_fires_before_the_key_check(
        self,
        client: httpx.AsyncClient,
        external_env: Callable[..., Any],
        mailbox: MailAccount,
    ) -> None:
        """ORDER: ``consume(LIMIT_EXTERNAL_WRITE, ip)`` runs FIRST (anti-flood).

        With a capacity of 1, the first UNAUTHENTICATED call still consumes the
        budget (401) and the second is rejected with 429 — proving a failed-auth
        flood is rate-limited too (ADR-0029 §4 / ADR-0039 §1).
        """
        external_env(key=TEST_API_KEY, write_enabled=True, write_rate=1)

        first = await client.post(_url(mailbox.id), json=_body())
        assert first.status_code == 401  # budget consumed by an unauthenticated call

        second = await client.post(_url(mailbox.id), json=_body())
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in second.headers

    async def test_rate_limit_429_precedes_the_body_parse(
        self,
        client: httpx.AsyncClient,
        external_env: Callable[..., Any],
        mailbox: MailAccount,
    ) -> None:
        external_env(key=TEST_API_KEY, write_enabled=True, write_rate=1)
        first = await client.post(
            _url(mailbox.id), json=_body(), headers={"X-API-Key": TEST_API_KEY}
        )
        assert first.status_code in (200, 502)  # budget consumed either way

        # Same IP, garbage body: the 429 must win over the 400.
        second = await client.post(
            _url(mailbox.id),
            json={"nonsense": True},
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert second.status_code == 429


# ===========================================================================
# 3. 404 = the MAILBOX is unknown (ADR-0048 §4) — never a 400
# ===========================================================================


class TestUnknownMailbox:
    async def test_unknown_mailbox_404(
        self, client: httpx.AsyncClient, write_on: str, stub_smtp: dict[str, Any]
    ) -> None:
        resp = await client.post(_url(999_999), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        assert stub_smtp["smtp_calls"] == 0  # nothing went on the wire

    async def test_zero_id_is_404_not_400(
        self, client: httpx.AsyncClient, write_on: str, stub_smtp: dict[str, Any]
    ) -> None:
        # ``account_id`` is a plain ``int`` path param (no ``ge=1``) precisely so an
        # id < 1 resolves to "no such mailbox" (404), not a pre-auth 400.
        resp = await client.post(_url(0), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 404


# ===========================================================================
# 4. Validation → 400 (never a 500) — ADR-0048 §1 / §4f-send
# ===========================================================================


class TestValidation400:
    @pytest.mark.parametrize(
        ("case", "body"),
        [
            ("empty_body_text", _body(body_text="")),
            ("whitespace_only_body_text", _body(body_text="   \n\t ")),
            ("body_text_over_1mib", _body(body_text="x" * (1_048_576 + 1))),
            ("invalid_to_address", _body(to=["not-an-email"])),
            ("invalid_cc_address", _body(cc=["nope@"])),
            ("subject_over_998", _body(subject="s" * 999)),
            ("no_recipient_at_all", _body(to=[], cc=[])),
            ("no_recipient_cc_null", _body(to=[], cc=None)),
            (
                "too_many_recipients_over_the_union",
                # 60 + 50 = 110 > 100: the ceiling is on the SUM of to+cc, the
                # per-field limit alone would allow 200 (ADR-0048 §1).
                _body(
                    to=[f"a{i}@x.com" for i in range(60)],
                    cc=[f"b{i}@x.com" for i in range(50)],
                ),
            ),
            ("missing_body_text_field", {"to": ["a@x.com"]}),
            ("missing_to_field", {"body_text": "hi"}),
        ],
    )
    async def test_invalid_body_is_400_validation_error(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
        case: str,
        body: dict[str, Any],
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=body, headers={"X-API-Key": write_on})
        assert resp.status_code == 400, f"{case}: {resp.status_code} {resp.text[:200]}"
        assert resp.json()["error"]["code"] == "validation_error"
        assert stub_smtp["smtp_calls"] == 0  # never an SMTP round-trip on a bad body

    @pytest.mark.parametrize(
        ("case", "body"),
        [
            # Header INJECTION: a bare CR/LF (no continuation WSP) is neither a valid
            # fold nor a sane value — it is refused as 400, NOT silently sanitised and
            # NOT allowed to blow up as a 500 inside ``EmailMessage`` (which raises
            # ValueError on a linefeed in a header).
            ("subject_bare_lf", _body(subject="Re: hi\nBcc: evil@x.com")),
            ("subject_bare_cr", _body(subject="Re: hi\rBcc: evil@x.com")),
            ("subject_crlf", _body(subject="Re: hi\r\nBcc: evil@x.com")),
            ("in_reply_to_bare_lf", _body(in_reply_to="<a@x.com>\nX-Evil: 1")),
            ("refs_bare_crlf", _body(refs="<a@x.com>\r\n<b@x.com>")),  # newline, no WSP
        ],
    )
    async def test_header_injection_is_400_not_500(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
        case: str,
        body: dict[str, Any],
    ) -> None:
        resp = await client.post(_url(mailbox.id), json=body, headers={"X-API-Key": write_on})
        assert resp.status_code == 400, f"{case}: {resp.status_code} {resp.text[:200]}"
        assert resp.json()["error"]["code"] == "validation_error"
        assert stub_smtp["smtp_calls"] == 0


# ===========================================================================
# 5. HEADLINE — real FOLDED threading headers must go through intact
# ===========================================================================


class TestFoldedThreadingHeaders:
    """The prod-bug regression gate (ADR-0048 §1: headers are written EXACTLY as passed).

    A long thread's ``References`` arrives from the CRM **folded** — RFC 5322 §2.2.3
    splits a long header over several lines, each continuation starting with WSP
    (``\\r\\n`` + SP/HTAB). That is the SAME header value in wire form, not a broken
    one: it must be unfolded and sent, never rejected and never truncated. Rejecting
    it (or dropping identifiers) breaks "reply" on every long conversation — the exact
    failure this suite exists to prevent.
    """

    _FOLDED_REFS = "<a@x.com>\r\n <b@x.com>\r\n\t<c@x.com>"

    async def test_folded_references_is_accepted_and_unfolded(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(
            _url(mailbox.id),
            json=_body(in_reply_to="<c@x.com>", refs=self._FOLDED_REFS),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200, resp.text

        msg: EmailMessage = stub_smtp["msg"]
        refs = msg["References"]
        # Unfolded: one logical line, no bare CR/LF left in the value we set.
        assert "\r" not in refs and "\n" not in refs
        # NOT ONE identifier lost — this is the whole point.
        for mid in ("<a@x.com>", "<b@x.com>", "<c@x.com>"):
            assert mid in refs, f"{mid} lost from References"
        assert refs == "<a@x.com> <b@x.com> <c@x.com>"
        assert msg["In-Reply-To"] == "<c@x.com>"

    async def test_all_message_ids_survive_onto_the_wire(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        """Serialise the MIME the way the SMTP client does: every id still there.

        (``policy.SMTP`` may RE-fold the long header for the wire — that is legal
        RFC folding; what must not happen is a lost or mangled identifier.)
        """
        resp = await client.post(
            _url(mailbox.id),
            json=_body(refs=self._FOLDED_REFS),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200

        raw = bytes(stub_smtp["msg"]).decode("utf-8", "replace")
        for mid in ("<a@x.com>", "<b@x.com>", "<c@x.com>"):
            assert mid in raw, f"{mid} lost on the wire"

    async def test_long_real_world_thread_references(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        # A 12-deep thread, folded every entry — the shape an old conversation
        # actually has.
        ids = [f"<msg{i}@thread.example.com>" for i in range(12)]
        folded = "\r\n ".join(ids)

        resp = await client.post(
            _url(mailbox.id),
            json=_body(in_reply_to=ids[-1], refs=folded),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200, resp.text

        refs = stub_smtp["msg"]["References"]
        for mid in ids:
            assert mid in refs
        assert refs.count("@thread.example.com") == 12

    async def test_folded_subject_is_accepted_and_unfolded(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        # Inbound mail carries folded subjects; the CRM builds ``"Re: " + <stored>``.
        # A FOLD is legal (continuation starts with WSP) → unfolded and sent, 200.
        resp = await client.post(
            _url(mailbox.id),
            json=_body(subject="Re: a very long subject\r\n that was folded"),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200, resp.text
        subject = stub_smtp["msg"]["Subject"]
        assert subject == "Re: a very long subject that was folded"
        assert "\r" not in subject and "\n" not in subject

    async def test_plain_unfolded_references_still_work(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        resp = await client.post(
            _url(mailbox.id),
            json=_body(refs="<a@x.com> <b@x.com>", in_reply_to="<b@x.com>"),
            headers={"X-API-Key": write_on},
        )
        assert resp.status_code == 200
        assert stub_smtp["msg"]["References"] == "<a@x.com> <b@x.com>"

    async def test_no_threading_headers_when_not_passed(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        stub_smtp: dict[str, Any],
    ) -> None:
        # The aggregator NEVER synthesises threading headers (ADR-0048 §1).
        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 200
        msg: EmailMessage = stub_smtp["msg"]
        assert msg["References"] is None
        assert msg["In-Reply-To"] is None


# ===========================================================================
# 6. SMTP failure → 502 (the remote rejected / did not answer)
# ===========================================================================


class TestSmtpFailure:
    async def test_smtp_error_is_502_smtp_failed(
        self,
        client: httpx.AsyncClient,
        write_on: str,
        mailbox: MailAccount,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import aiosmtplib

        async def _boom(*_a: Any, **_k: Any) -> None:
            raise aiosmtplib.SMTPConnectError("smtp down")

        monkeypatch.setattr(aiosmtplib, "send", _boom)

        resp = await client.post(_url(mailbox.id), json=_body(), headers={"X-API-Key": write_on})
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "smtp_failed"
