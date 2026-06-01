"""Bug-fix regression: editing / clearing the display_name (nickname) of an
OAuth (``oauth_outlook``) mail account.

Source of truth:

- backend/app/accounts/service.py :: ``MailAccountService.update``
  (ADR-0025 §4c — oauth accounts allow changing *only* the display name; a
  no-op snapshot of the shared edit form, where every credential/host field
  equals the stored value, must NOT be treated as a forbidden credential
  change and must NOT trigger a login probe).
- backend/app/accounts/schemas.py :: ``MailAccountUpdateRequest``
  (``clear_display_name`` sentinel + ``display_name`` trim validator).
- docs ADR-0025 §4c / docs/04-api-contracts (no-op snapshot == not an error).

The bug: the shared edit form posts a *full snapshot*
(imap_host/port/ssl + smtp_host/port/ssl/starttls/username + display_name).
The old guard rejected the snapshot as a credential change, so renaming an
OAuth mailbox 400'd. The fix: a field counts as a forbidden change only when
it is provided (not None) AND differs from the stored value. ``password`` /
``smtp_password`` stay forbidden whenever a non-empty value is sent.

These tests go through the live FastAPI app (``oauth_client``). The seed
helpers insert oauth/password rows directly; the IMAP/SMTP login probes are
mocked and *call-counted* so we can prove the probe is (not) invoked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from backend.app.repositories.mail_accounts import MailAccountsRepo
from backend.app.repositories.users import UsersRepo
from shared.config import get_settings
from shared.crypto import MailPasswordCipher, decrypt_mail_password
from shared.db import make_session
from tests.oauth.conftest import two_step_login

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Login / seed helpers (mirror tests/oauth/test_oauth_endpoints.py).
# ---------------------------------------------------------------------------


async def _login_admin(client: httpx.AsyncClient) -> str:
    s = get_settings()
    resp = await two_step_login(client, s.ADMIN_LOGIN, s.ADMIN_PASSWORD)
    assert resp.status_code in (302, 303), resp.text
    csrf = resp.cookies.get("mas_csrf")
    assert csrf
    return csrf


async def _admin_id() -> int:
    async with make_session() as s:
        admin = await UsersRepo(s).get_admin()
    assert admin is not None
    return admin.id


# Canonical Microsoft host/port for an Outlook OAuth mailbox. The shared edit
# form submits exactly these as the "snapshot" of an unchanged account.
OAUTH_IMAP_HOST = "outlook.office365.com"
OAUTH_IMAP_PORT = 993
OAUTH_IMAP_SSL = True
OAUTH_SMTP_HOST = "smtp-mail.outlook.com"
OAUTH_SMTP_PORT = 587
OAUTH_SMTP_SSL = False
OAUTH_SMTP_STARTTLS = True


async def _seed_oauth_account(
    *,
    user_id: int,
    email: str = "box@outlook.com",
    display_name: str | None = None,
) -> int:
    async with make_session() as s, s.begin():
        repo = MailAccountsRepo(s)
        acc_id = await repo.next_account_id()
        cipher = MailPasswordCipher.from_settings()
        await repo.insert_oauth_account_with_id(
            account_id=acc_id,
            user_id=user_id,
            group_id=None,
            email=email,
            oauth_provider="outlook",
            oauth_refresh_token_encrypted=cipher.encrypt("RT", acc_id),
            oauth_access_token_encrypted=cipher.encrypt("AT-cached", acc_id),
            oauth_access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            oauth_scopes="scope",
            imap_host=OAUTH_IMAP_HOST,
            imap_port=OAUTH_IMAP_PORT,
            imap_ssl=OAUTH_IMAP_SSL,
            smtp_host=OAUTH_SMTP_HOST,
            smtp_port=OAUTH_SMTP_PORT,
            smtp_ssl=OAUTH_SMTP_SSL,
            smtp_starttls=OAUTH_SMTP_STARTTLS,
        )
        if display_name is not None:
            await repo.update_fields(acc_id, display_name=display_name)
    return acc_id


PW_IMAP_HOST = "imap.example.com"
PW_IMAP_PORT = 993
PW_IMAP_SSL = True
PW_SMTP_HOST = "smtp.example.com"
PW_SMTP_PORT = 465
PW_SMTP_SSL = True
PW_SMTP_STARTTLS = False


async def _seed_password_account(
    *,
    user_id: int,
    email: str = "pw@example.com",
    display_name: str | None = None,
) -> int:
    async with make_session() as s, s.begin():
        repo = MailAccountsRepo(s)
        acc_id = await repo.next_account_id()
        cipher = MailPasswordCipher.from_settings()
        await repo.insert_with_id(
            account_id=acc_id,
            user_id=user_id,
            group_id=None,
            email=email,
            encrypted_password=cipher.encrypt("orig-pwd", acc_id),
            imap_host=PW_IMAP_HOST,
            imap_port=PW_IMAP_PORT,
            imap_ssl=PW_IMAP_SSL,
            smtp_host=PW_SMTP_HOST,
            smtp_port=PW_SMTP_PORT,
            smtp_ssl=PW_SMTP_SSL,
            smtp_starttls=PW_SMTP_STARTTLS,
            smtp_username=None,
            smtp_encrypted_password=None,
        )
        if display_name is not None:
            await repo.update_fields(acc_id, display_name=display_name)
    return acc_id


def _oauth_form_snapshot(**overrides: Any) -> dict[str, Any]:
    """Full edit-form snapshot equal to the seeded OAuth account's values.

    Mirrors what the shared edit form submits when the user only touches the
    nickname. Overrides win (used to flip a single field to a *different*
    value in scenario B).
    """
    snap: dict[str, Any] = {
        "imap_host": OAUTH_IMAP_HOST,
        "imap_port": OAUTH_IMAP_PORT,
        "imap_ssl": OAUTH_IMAP_SSL,
        "smtp_host": OAUTH_SMTP_HOST,
        "smtp_port": OAUTH_SMTP_PORT,
        "smtp_ssl": OAUTH_SMTP_SSL,
        "smtp_starttls": OAUTH_SMTP_STARTTLS,
    }
    snap.update(overrides)
    return snap


def _pw_form_snapshot(**overrides: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "imap_host": PW_IMAP_HOST,
        "imap_port": PW_IMAP_PORT,
        "imap_ssl": PW_IMAP_SSL,
        "smtp_host": PW_SMTP_HOST,
        "smtp_port": PW_SMTP_PORT,
        "smtp_ssl": PW_SMTP_SSL,
        "smtp_starttls": PW_SMTP_STARTTLS,
    }
    snap.update(overrides)
    return snap


@pytest.fixture
def _probe_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every IMAP/SMTP login probe so a test can prove it ran (or did
    not). The login probes are the only thing that touches the network; if a
    bare display_name edit triggers them, the bug is back.
    """
    from backend.app.accounts import service as svc_mod

    calls = {"imap": 0, "smtp": 0}

    async def _ok_imap(**_: Any) -> None:
        calls["imap"] += 1

    async def _ok_smtp(**_: Any) -> None:
        calls["smtp"] += 1

    monkeypatch.setattr(svc_mod, "imap_test_login", _ok_imap)
    monkeypatch.setattr(svc_mod, "smtp_test_login", _ok_smtp)
    return calls


async def _patch(
    client: httpx.AsyncClient, acc_id: int, csrf: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.request(
        "PATCH",
        f"/api/mail-accounts/{acc_id}",
        json=body,
        headers={"X-CSRF-Token": csrf},
    )


async def _get_account(acc_id: int):  # type: ignore[no-untyped-def]
    async with make_session() as s:
        return await MailAccountsRepo(s).get_by_id(acc_id)


# ===========================================================================
# A. THE BUG: rename OAuth account with a full no-op form snapshot -> 200.
# ===========================================================================


class TestOAuthNoOpSnapshotRename:
    async def test_rename_with_full_snapshot_succeeds_without_probe(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        """A. display_name='Ник' + full snapshot equal to current -> 200,
        display_name updated, NO login probe. This is the regression that
        was a 400 before the fix.
        """
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())

        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"display_name": "Ник", **_oauth_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "Ник"

        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Ник"
        # Hosts untouched.
        assert acc.imap_host == OAUTH_IMAP_HOST
        assert acc.smtp_host == OAUTH_SMTP_HOST
        # The OAuth path never re-probes IMAP/SMTP.
        assert _probe_spy == {"imap": 0, "smtp": 0}

    async def test_snapshot_only_no_display_name_is_noop_200(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        """Full snapshot with no display_name key at all -> 200, nickname
        unchanged, no probe.
        """
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id(), display_name="Keep")

        resp = await _patch(oauth_client, acc_id, csrf, _oauth_form_snapshot())
        assert resp.status_code == 200, resp.text

        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Keep"
        assert _probe_spy == {"imap": 0, "smtp": 0}


# ===========================================================================
# B. ADR-0025 §4c contract intact: a REAL credential/host change -> 400.
# ===========================================================================


class TestOAuthRealCredentialChangeRejected:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("imap_host", "evil.example.com"),
            ("imap_port", 1234),
            ("imap_ssl", False),
            ("smtp_host", "evil-smtp.example.com"),
            ("smtp_port", 2525),
            ("smtp_ssl", True),  # also flips starttls reality; still a change
            ("smtp_starttls", False),
            ("smtp_username", "someone-else"),
            ("email", "other@outlook.com"),
        ],
    )
    async def test_changing_one_real_field_returns_400_auth_type(
        self,
        oauth_client: httpx.AsyncClient,
        _probe_spy: dict[str, int],
        field: str,
        value: Any,
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())

        # Snapshot with exactly one field changed to a DIFFERENT value, plus a
        # nickname (proving the nickname alone can't rescue a real change).
        body = {"display_name": "X", **_oauth_form_snapshot(**{field: value})}
        resp = await _patch(oauth_client, acc_id, csrf, body)

        assert resp.status_code == 400, resp.text
        err = resp.json()["error"]
        assert err["code"] == "validation_error"
        assert err["field"] == "auth_type"
        # No probe; nothing persisted.
        assert _probe_spy == {"imap": 0, "smtp": 0}
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name is None  # rename rolled back with the 400
        assert acc.imap_host == OAUTH_IMAP_HOST


# ===========================================================================
# C. OAuth has no password: any non-empty password / smtp_password -> 400.
# ===========================================================================


class TestOAuthPasswordRejected:
    async def test_nonempty_password_returns_400(self, oauth_client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())
        resp = await _patch(
            oauth_client, acc_id, csrf, {"password": "hunter2", **_oauth_form_snapshot()}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["field"] == "auth_type"

    async def test_nonempty_smtp_password_returns_400(
        self, oauth_client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())
        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"smtp_password": "hunter2", **_oauth_form_snapshot()},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["field"] == "auth_type"

    async def test_empty_password_string_is_allowed(self, oauth_client: httpx.AsyncClient) -> None:
        """An empty-string password (form sends '') is falsy -> NOT a change."""
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())
        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"display_name": "Y", "password": "", "smtp_password": "", **_oauth_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Y"


# ===========================================================================
# D. The JSON happy paths: set + clear the nickname.
# ===========================================================================


class TestOAuthDisplayNameSetAndClear:
    async def test_bare_display_name_only_200(self, oauth_client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id())
        resp = await _patch(oauth_client, acc_id, csrf, {"display_name": "Solo"})
        assert resp.status_code == 200, resp.text
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Solo"

    async def test_clear_display_name_sets_null(self, oauth_client: httpx.AsyncClient) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id(), display_name="ToClear")
        resp = await _patch(oauth_client, acc_id, csrf, {"clear_display_name": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] is None
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name is None

    async def test_clear_display_name_with_full_snapshot_sets_null(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        """Clear via the real form path: clear flag + full snapshot -> NULL,
        no probe.
        """
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id(), display_name="ToClear")
        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"clear_display_name": True, **_oauth_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name is None
        assert _probe_spy == {"imap": 0, "smtp": 0}


# ===========================================================================
# E. Backend behaviour on {display_name: null} (the OLD frontend payload that
#    caused the bug). Backend treats JSON null as "field not provided" -> it
#    must NOT crash and must NOT clear. Documents the actual contract.
# ===========================================================================


class TestOAuthDisplayNameNullIgnored:
    async def test_display_name_null_does_not_clear_and_does_not_500(
        self, oauth_client: httpx.AsyncClient
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id(), display_name="Stays")

        resp = await _patch(oauth_client, acc_id, csrf, {"display_name": None})
        # No crash.
        assert resp.status_code == 200, resp.text
        # JSON null == "not provided"; clear_display_name defaults False, so
        # the nickname is preserved (this was the old FE bug: the FE expected
        # null to clear, but the backend ignores it — FE now sends
        # clear_display_name instead, see scenario D).
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Stays"

    async def test_display_name_null_with_snapshot_preserves_value(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_oauth_account(user_id=await _admin_id(), display_name="Stays")
        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"display_name": None, **_oauth_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Stays"
        assert _probe_spy == {"imap": 0, "smtp": 0}


# ===========================================================================
# F. Password-account regression: the snapshot-without-password edit must not
#    probe / reset failures; password change still works; clear works.
# ===========================================================================


class TestPasswordAccountRegression:
    async def test_snapshot_without_password_does_not_probe_or_reset_failures(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_password_account(user_id=await _admin_id())
        # Mark the account unhealthy so we can prove a bare rename leaves the
        # health state untouched.
        async with make_session() as s, s.begin():
            await MailAccountsRepo(s).update_fields(
                acc_id, is_active=False, last_sync_error="boom", consecutive_failures=5
            )

        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"display_name": "Renamed", **_pw_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text

        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name == "Renamed"
        # No login probe ran (no password submitted).
        assert _probe_spy == {"imap": 0, "smtp": 0}
        # Health state NOT reset by a bare rename.
        assert acc.is_active is False
        assert acc.last_sync_error == "boom"
        assert acc.consecutive_failures == 5

    async def test_password_change_runs_probe_and_reactivates(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_password_account(user_id=await _admin_id())
        async with make_session() as s, s.begin():
            await MailAccountsRepo(s).update_fields(
                acc_id, is_active=False, last_sync_error="boom", consecutive_failures=5
            )

        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"password": "new-pwd", **_pw_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text

        # Probe ran for the new password; health reset.
        assert _probe_spy["imap"] >= 1
        assert _probe_spy["smtp"] >= 1
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.is_active is True
        assert acc.last_sync_error is None
        assert acc.consecutive_failures == 0
        # New password actually persisted (re-encrypted).
        assert acc.encrypted_password is not None
        assert decrypt_mail_password(acc.encrypted_password, acc.id) == "new-pwd"

    async def test_clear_display_name_on_password_account(
        self, oauth_client: httpx.AsyncClient, _probe_spy: dict[str, int]
    ) -> None:
        csrf = await _login_admin(oauth_client)
        acc_id = await _seed_password_account(user_id=await _admin_id(), display_name="Nick")
        resp = await _patch(
            oauth_client,
            acc_id,
            csrf,
            {"clear_display_name": True, **_pw_form_snapshot()},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] is None
        acc = await _get_account(acc_id)
        assert acc is not None
        assert acc.display_name is None
        # Bare clear (no password) must not probe.
        assert _probe_spy == {"imap": 0, "smtp": 0}
