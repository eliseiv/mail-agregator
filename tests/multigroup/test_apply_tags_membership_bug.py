"""REPRO: apply-tags visibility ignores additional memberships (ADR-0030).

ADR-0030 supersedes the single-group predicate ``u.group_id = ma.group_id``
**everywhere visibility is decided**, and its superseding note explicitly
lists ``ADR-0017 §5/§5.1 — apply-tags visibility`` ("теги навешиваются всем
видящим письмо: owner + члены команды ящика через ``user_groups`` +
super_admin").

The recipient SQL (``telegram_notifications``) and the webhook tag-predicate
WERE migrated to a ``user_groups`` membership check. But
``backend/app/tags/sql.py`` (``APPLY_TAGS_TO_MESSAGE`` line ~198 and
``APPLY_TAG_TO_EXISTING`` line ~273) still use the single-team predicate.

Effect: a member of teams [A, B] (home A, additional B) does NOT get their
tags auto-applied to mail on team B's mailbox. Because tags drive both
Telegram notifications and webhooks, the multi-team member silently loses
notifications/webhooks for their additional team's mail — breaking ADR-0030
§2 (visibility consistent with notifications).

This test is expected to FAIL until ``tags/sql.py`` is fixed to use a
``user_groups`` membership check (blame: code).
"""

from __future__ import annotations

import pytest

from tests.multigroup.conftest import MultiGroupSeeder

pytestmark = pytest.mark.integration


async def test_apply_tags_to_message_attaches_via_additional_membership(
    mseed: MultiGroupSeeder,
) -> None:
    ga, _la = await mseed.group_with_leader("Team A")
    gb, _lb = await mseed.group_with_leader("Team B")
    # multi-team member: home A, additional B.
    m = await mseed.member(ga.id)
    await mseed.membership(user_id=m.id, group_id=gb.id)

    # Mailbox belongs to team B (the member's ADDITIONAL team).
    acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="bugb@x.com")
    msg_b = await mseed.message(mail_account_id=acc_b.id, body_text="please pay the invoice")
    tag = await mseed.tag(user_id=m.id, name="bug-inv", rules=[("body_contains", "invoice")])

    await mseed.apply_tags_to_message(message=msg_b, mail_account_id=acc_b.id)

    assert tag.id in await mseed.tags_on_message(msg_b.id), (
        "ADR-0030: a member's tag must auto-attach to mail on ANY of their teams. "
        "BUG: backend/app/tags/sql.py:APPLY_TAGS_TO_MESSAGE uses "
        "`u.group_id = ma.group_id` (home only) instead of a user_groups "
        "membership check."
    )


async def test_apply_tag_to_existing_attaches_via_additional_membership(
    mseed: MultiGroupSeeder,
) -> None:
    """apply-to-existing (POST /api/tags?apply_to_existing) for a member of
    [A, B] must tag pre-existing mail on team B's mailbox.
    """
    ga, _la = await mseed.group_with_leader("Team A")
    gb, _lb = await mseed.group_with_leader("Team B")
    m = await mseed.member(ga.id)
    await mseed.membership(user_id=m.id, group_id=gb.id)

    acc_b = await mseed.mail_account(user_id=_lb.id, group_id=gb.id, email="bugexist@x.com")
    msg_b = await mseed.message(mail_account_id=acc_b.id, body_text="contract to review")
    tag = await mseed.tag(user_id=m.id, name="bug-con", rules=[("body_contains", "contract")])

    # APPLY_TAG_TO_EXISTING only takes a SINGLE :user_group_id (home), so even
    # if called with the home group it cannot reach the additional team's mail.
    await mseed.apply_tag_to_existing(
        tag_id=tag.id,
        user_id=m.id,
        user_group_id=ga.id,  # home team — the only value the query accepts
        is_super_admin=False,
    )

    assert tag.id in await mseed.tags_on_message(msg_b.id), (
        "ADR-0030: apply-to-existing must cover ALL of the user's teams. "
        "BUG: backend/app/tags/sql.py:APPLY_TAG_TO_EXISTING takes a single "
        "`:user_group_id` (home) and matches `ma.group_id = :user_group_id`, "
        "so additional-team mail is never tagged."
    )
