"""A. Migration 018 (outlook_oauth2) — schema + CHECK-constraint behaviour.

The shared test DB is already at head (``20260527_018``), so the columns and
constraints below prove ``upgrade()`` applied cleanly over 017. We then assert
the three CHECK constraints by attempting raw INSERTs:

- password account without ``encrypted_password`` -> reject
- oauth account without ``oauth_refresh_token_encrypted`` -> reject
- bogus ``auth_type`` -> reject
- valid oauth account (NULL password) -> accept

The downgrade round-trip is covered separately by
``test_migration_018_downgrade.py`` which runs its own throwaway Postgres
schema so it never mutates the shared DB other tests rely on.

These tests use raw SQL (not the ORM) so we exercise the DB constraints
directly, independent of any SQLAlchemy-side validation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _seed_user(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO users (username, password_hash, role, created_at, updated_at) "
            "VALUES ('mig_user', 'x', 'group_member', now(), now()) RETURNING id"
        )
    )
    return int(row.scalar_one())


class TestColumnsExist:
    async def test_oauth_columns_present(self, db_session: AsyncSession) -> None:
        rows = await db_session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'mail_accounts'"
            )
        )
        cols = {r[0]: r[1] for r in rows}
        for c in (
            "auth_type",
            "oauth_provider",
            "oauth_refresh_token_encrypted",
            "oauth_access_token_encrypted",
            "oauth_access_token_expires_at",
            "oauth_needs_consent",
            "oauth_scopes",
            "proxy_url",
        ):
            assert c in cols, f"missing column {c}"

    async def test_encrypted_password_is_nullable(self, db_session: AsyncSession) -> None:
        row = await db_session.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='mail_accounts' AND column_name='encrypted_password'"
            )
        )
        assert row.scalar_one() == "YES"

    async def test_check_constraints_present(self, db_session: AsyncSession) -> None:
        rows = await db_session.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'mail_accounts'::regclass AND contype = 'c'"
            )
        )
        names = {r[0] for r in rows}
        assert "ck_mail_accounts_auth_type" in names
        assert "ck_mail_accounts_password_creds" in names
        assert "ck_mail_accounts_oauth_creds" in names


class TestCheckConstraints:
    async def test_password_account_without_password_rejected(
        self, db_session: AsyncSession
    ) -> None:
        uid = await _seed_user(db_session)
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO mail_accounts "
                    "(user_id, email, auth_type, encrypted_password, "
                    " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                    " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                    " created_at, updated_at) "
                    "VALUES (:uid, 'a@b.com', 'password', NULL, "
                    " 'imap.x', 993, true, 'smtp.x', 465, true, false, true, 0, "
                    " now(), now())"
                ),
                {"uid": uid},
            )
        assert "ck_mail_accounts_password_creds" in str(exc.value)
        await db_session.rollback()

    async def test_oauth_account_without_refresh_token_rejected(
        self, db_session: AsyncSession
    ) -> None:
        uid = await _seed_user(db_session)
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO mail_accounts "
                    "(user_id, email, auth_type, oauth_provider, "
                    " oauth_refresh_token_encrypted, "
                    " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                    " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                    " created_at, updated_at) "
                    "VALUES (:uid, 'o@b.com', 'oauth_outlook', 'outlook', NULL, "
                    " 'outlook.office365.com', 993, true, 'smtp-mail.outlook.com', "
                    " 587, false, true, true, 0, now(), now())"
                ),
                {"uid": uid},
            )
        assert "ck_mail_accounts_oauth_creds" in str(exc.value)
        await db_session.rollback()

    async def test_oauth_account_wrong_provider_rejected(self, db_session: AsyncSession) -> None:
        uid = await _seed_user(db_session)
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO mail_accounts "
                    "(user_id, email, auth_type, oauth_provider, "
                    " oauth_refresh_token_encrypted, "
                    " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                    " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                    " created_at, updated_at) "
                    "VALUES (:uid, 'o@b.com', 'oauth_outlook', 'gmail', "
                    " '\\x0102'::bytea, "
                    " 'outlook.office365.com', 993, true, 'smtp-mail.outlook.com', "
                    " 587, false, true, true, 0, now(), now())"
                ),
                {"uid": uid},
            )
        assert "ck_mail_accounts_oauth_creds" in str(exc.value)
        await db_session.rollback()

    async def test_bogus_auth_type_rejected(self, db_session: AsyncSession) -> None:
        uid = await _seed_user(db_session)
        with pytest.raises(IntegrityError) as exc:
            await db_session.execute(
                text(
                    "INSERT INTO mail_accounts "
                    "(user_id, email, auth_type, encrypted_password, "
                    " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                    " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                    " created_at, updated_at) "
                    "VALUES (:uid, 'a@b.com', 'imap_basic', '\\x01'::bytea, "
                    " 'imap.x', 993, true, 'smtp.x', 465, true, false, true, 0, "
                    " now(), now())"
                ),
                {"uid": uid},
            )
        assert "ck_mail_accounts_auth_type" in str(exc.value)
        await db_session.rollback()

    async def test_valid_oauth_account_accepted(self, db_session: AsyncSession) -> None:
        uid = await _seed_user(db_session)
        row = await db_session.execute(
            text(
                "INSERT INTO mail_accounts "
                "(user_id, email, auth_type, oauth_provider, "
                " oauth_refresh_token_encrypted, encrypted_password, "
                " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                " created_at, updated_at) "
                "VALUES (:uid, 'ok@b.com', 'oauth_outlook', 'outlook', "
                " '\\x0102'::bytea, NULL, "
                " 'outlook.office365.com', 993, true, 'smtp-mail.outlook.com', "
                " 587, false, true, true, 0, now(), now()) RETURNING id, auth_type"
            ),
            {"uid": uid},
        )
        rid, auth_type = row.one()
        assert rid > 0
        assert auth_type == "oauth_outlook"
        await db_session.rollback()

    async def test_valid_password_account_accepted(self, db_session: AsyncSession) -> None:
        uid = await _seed_user(db_session)
        row = await db_session.execute(
            text(
                "INSERT INTO mail_accounts "
                "(user_id, email, auth_type, encrypted_password, "
                " imap_host, imap_port, imap_ssl, smtp_host, smtp_port, "
                " smtp_ssl, smtp_starttls, is_active, consecutive_failures, "
                " created_at, updated_at) "
                "VALUES (:uid, 'pw@b.com', 'password', '\\x0102'::bytea, "
                " 'imap.x', 993, true, 'smtp.x', 465, true, false, true, 0, "
                " now(), now()) RETURNING id"
            ),
            {"uid": uid},
        )
        assert int(row.scalar_one()) > 0
        await db_session.rollback()
