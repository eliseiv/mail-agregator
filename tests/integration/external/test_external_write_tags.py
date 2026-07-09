"""External WRITE-API — global tag catalogue (ADR-0040 §4, ``docs/04-api-contracts.md`` §4f).

Covers the global-tag CRUD reached by the headless CRM through the external key:

- ``POST /tags``                create → 201; 409 on a duplicate name.
- ``DELETE /tags/{id}``         builtin → **409 conflict** (external contract);
                                the internal UI ``DELETE /api/tags/{id}`` for a
                                builtin returns **400 cannot_delete_builtin_tag**
                                — the two codes DIFFER (both asserted).
- ``POST/DELETE /tags/{id}/rules``  add / delete a rule.
- ``POST /tags/{id}/apply-to-existing``  applies to EVERY message (global reach),
                                idempotent (``ON CONFLICT DO NOTHING``);
                                ``422 tag_apply_too_many`` over the corpus limit.

Global-tag application semantics (LEFT JOIN + ``t.user_id IS NULL`` branch) and
the ADR-0017 whole-word/case-sensitive matching live in
``test_global_tags_application.py``. Personal-tag / super_admin behaviour is
asserted here to be UNCHANGED by the global-tag work (silent-regression guard).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from backend.app.exceptions import CannotDeleteBuiltinTagError, ConflictError
from backend.app.tags.service import TagsService
from shared.models import Tag, User

pytestmark = pytest.mark.integration

_TAGS = "/api/external/tags"
_COLOR = "#2563eb"


async def _builtin_global_tag_id(client: httpx.AsyncClient, key: str) -> int:
    """Return the id of a seeded builtin GLOBAL tag via the read catalogue."""
    resp = await client.get(_TAGS, headers={"X-API-Key": key})
    assert resp.status_code == 200, resp.text
    builtins = [t for t in resp.json()["tags"] if t["is_builtin"]]
    assert builtins, "app lifespan must seed builtin global tags (seed_builtin_tags)"
    return int(builtins[0]["id"])


class TestCreate:
    async def test_create_returns_201_full_dto(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.post(
            _TAGS,
            headers={"X-API-Key": write_api_on},
            json={"name": "CRM-Priority", "color": _COLOR},
        )
        assert resp.status_code == 201, resp.text
        dto = resp.json()
        assert dto["name"] == "CRM-Priority"
        assert dto["is_builtin"] is False
        assert dto["match_mode"] == "any"  # default
        assert dto["rules"] == []

    async def test_duplicate_name_is_409(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        body = {"name": "UniqueOne", "color": _COLOR}
        r1 = await client.post(_TAGS, headers={"X-API-Key": write_api_on}, json=body)
        assert r1.status_code == 201, r1.text
        r2 = await client.post(_TAGS, headers={"X-API-Key": write_api_on}, json=body)
        assert r2.status_code == 409, r2.text
        assert r2.json()["error"]["code"] == "conflict"

    async def test_clash_with_builtin_global_name_is_409(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        """A create colliding with a seeded builtin global name → 409 (the
        partial-unique ``uq_tags_global_name`` covers builtin + custom alike)."""
        cat = await client.get(_TAGS, headers={"X-API-Key": write_api_on})
        builtin_name = next(t["name"] for t in cat.json()["tags"] if t["is_builtin"])
        resp = await client.post(
            _TAGS, headers={"X-API-Key": write_api_on}, json={"name": builtin_name, "color": _COLOR}
        )
        assert resp.status_code == 409, resp.text


class TestDeleteBuiltinCodesDiffer:
    async def test_external_delete_builtin_is_409(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        tag_id = await _builtin_global_tag_id(client, write_api_on)
        resp = await client.delete(f"{_TAGS}/{tag_id}", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "conflict"

    async def test_internal_service_delete_builtin_is_400_cannot_delete(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        super_admin: User,
        db_engine: AsyncEngine,
    ) -> None:
        """The internal UI path (``TagsService.delete``) raises a DIFFERENT
        error for a builtin: ``CannotDeleteBuiltinTagError`` (400
        ``cannot_delete_builtin_tag``) — vs the external 409 above. Exercised at
        the service layer against a builtin PERSONAL tag (the internal delete is
        owner-scoped and the two-step UI login is round-6 tech-debt)."""
        # Seed a builtin personal tag owned by the super-admin.
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses, ses.begin():
            tag = Tag(
                user_id=super_admin.id, name="builtin-personal", color=_COLOR, is_builtin=True
            )
            ses.add(tag)
            await ses.flush()
            tag_id = tag.id

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            with pytest.raises(CannotDeleteBuiltinTagError) as ei:
                await TagsService(ses).delete(user_id=super_admin.id, tag_id=tag_id)
        assert ei.value.status_code == 400
        assert ei.value.code == "cannot_delete_builtin_tag"

    async def test_global_service_delete_builtin_is_409_conflict(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        db_engine: AsyncEngine,
    ) -> None:
        """Twin at the service layer: ``TagsService.delete_global`` raises
        ``ConflictError`` (409) for a builtin global tag — the code the external
        contract surfaces."""
        # Ensure builtins are seeded (lifespan ran via the client fixture).
        await client.get(_TAGS, headers={"X-API-Key": write_api_on})
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            builtin = (
                await ses.execute(
                    text("SELECT id FROM tags WHERE user_id IS NULL AND is_builtin = true LIMIT 1")
                )
            ).scalar_one()
        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        async with factory() as ses:
            with pytest.raises(ConflictError) as ei:
                await TagsService(ses).delete_global(tag_id=int(builtin))
        assert ei.value.status_code == 409


class TestCustomDeleteAndUpdate:
    async def test_delete_custom_global_tag_is_204(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        created = await client.post(
            _TAGS, headers={"X-API-Key": write_api_on}, json={"name": "ToDelete", "color": _COLOR}
        )
        tag_id = created.json()["id"]
        resp = await client.delete(f"{_TAGS}/{tag_id}", headers={"X-API-Key": write_api_on})
        assert resp.status_code == 204, resp.text

    async def test_update_unknown_tag_is_404(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.patch(
            f"{_TAGS}/987654", headers={"X-API-Key": write_api_on}, json={"name": "x"}
        )
        assert resp.status_code == 404, resp.text


class TestRules:
    async def test_add_and_delete_rule(self, client: httpx.AsyncClient, write_api_on: str) -> None:
        created = await client.post(
            _TAGS, headers={"X-API-Key": write_api_on}, json={"name": "WithRules", "color": _COLOR}
        )
        tag_id = created.json()["id"]
        add = await client.post(
            f"{_TAGS}/{tag_id}/rules",
            headers={"X-API-Key": write_api_on},
            json={"type": "subject_contains", "pattern": "Invoice"},
        )
        assert add.status_code == 201, add.text
        rule_id = add.json()["id"]
        assert add.json()["type"] == "subject_contains"
        assert add.json()["pattern"] == "Invoice"

        dele = await client.delete(
            f"{_TAGS}/{tag_id}/rules/{rule_id}", headers={"X-API-Key": write_api_on}
        )
        assert dele.status_code == 204, dele.text

    async def test_add_rule_to_unknown_tag_is_404(
        self, client: httpx.AsyncClient, write_api_on: str
    ) -> None:
        resp = await client.post(
            f"{_TAGS}/987654/rules",
            headers={"X-API-Key": write_api_on},
            json={"type": "subject_contains", "pattern": "x"},
        )
        assert resp.status_code == 404, resp.text


class TestApplyToExisting:
    async def _make_global_tag_with_rule(
        self, client: httpx.AsyncClient, key: str, *, name: str, pattern: str
    ) -> int:
        created = await client.post(
            _TAGS, headers={"X-API-Key": key}, json={"name": name, "color": _COLOR}
        )
        tag_id = created.json()["id"]
        await client.post(
            f"{_TAGS}/{tag_id}/rules",
            headers={"X-API-Key": key},
            json={"type": "subject_contains", "pattern": pattern},
        )
        return int(tag_id)

    async def test_apply_tags_all_matching_and_is_idempotent(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
    ) -> None:
        acc = await make_mail_account(super_admin.id, "apply@example.com")
        m1 = await make_message(acc.id, uid=1, subject="Weekly Report ready")
        await make_message(acc.id, uid=2, subject="unrelated subject")
        tag_id = await self._make_global_tag_with_rule(
            client, write_api_on, name="ReportTag", pattern="Report"
        )

        r1 = await client.post(
            f"{_TAGS}/{tag_id}/apply-to-existing", headers={"X-API-Key": write_api_on}
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["applied_count"] == 1, "only the matching message m1 is tagged"

        # Idempotent: a second apply inserts 0 new links (ON CONFLICT DO NOTHING).
        r2 = await client.post(
            f"{_TAGS}/{tag_id}/apply-to-existing", headers={"X-API-Key": write_api_on}
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["applied_count"] == 0
        del m1

    async def test_apply_over_limit_is_422_tag_apply_too_many(
        self,
        client: httpx.AsyncClient,
        write_api_on: str,
        super_admin: User,
        make_mail_account: Callable[..., Any],
        make_message: Callable[..., Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force the corpus guard by lowering ``APPLY_TO_EXISTING_LIMIT`` to 0 so
        even a single message trips ``422 tag_apply_too_many`` (ADR-0017 §7)."""
        acc = await make_mail_account(super_admin.id, "toobig@example.com")
        await make_message(acc.id, uid=1, subject="Report here")
        monkeypatch.setattr("backend.app.tags.service.APPLY_TO_EXISTING_LIMIT", 0)
        tag_id = await self._make_global_tag_with_rule(
            client, write_api_on, name="BigTag", pattern="Report"
        )
        resp = await client.post(
            f"{_TAGS}/{tag_id}/apply-to-existing", headers={"X-API-Key": write_api_on}
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "tag_apply_too_many"
