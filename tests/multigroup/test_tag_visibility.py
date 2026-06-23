"""Tag-filter visibility across memberships (ADR-0030 §2 / round-20).

``MessagesRepo.is_tag_visible_to_scope`` widened from "own team" to "any of
the caller's teams". A member of [A, B] must be able to filter the Inbox by a
tag owned by a B-team colleague (not 404), because the team-B messages that
tag scopes are visible to him.

Verification (plan: tag-visibility section):
- member of two teams can filter by a co-member's tag in the additional team;
- a tag owned by a user of a THIRD team is NOT visible (would 404 in the API).
"""

from __future__ import annotations

import pytest

from backend.app.repositories.messages import MessagesRepo
from tests.multigroup.conftest import MultiGroupSeeder

pytestmark = pytest.mark.integration


async def _visible(seed: MultiGroupSeeder, *, tag_id: int, user, group_ids):  # type: ignore[no-untyped-def]
    return await MessagesRepo(seed.s).is_tag_visible_to_scope(
        tag_id=tag_id,
        is_super_admin=False,
        user_id=user.id,
        group_ids=group_ids,
    )


class TestTagVisibilityAcrossTeams:
    async def test_member_of_two_teams_can_filter_by_colleagues_tag_in_extra_team(
        self, mseed: MultiGroupSeeder
    ) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        # multi-team member: home A, additional B.
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)
        # a colleague who is a member of team B.
        colleague_b = await mseed.member(gb.id)
        b_tag = await mseed.tag(user_id=colleague_b.id, name="b-team-tag")

        visible = await _visible(
            mseed, tag_id=b_tag.id, user=m, group_ids=frozenset({ga.id, gb.id})
        )
        assert visible is True, "B-colleague's tag must be visible to the [A,B] member"

    async def test_own_tag_visible(self, mseed: MultiGroupSeeder) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        m = await mseed.member(ga.id)
        own = await mseed.tag(user_id=m.id, name="own-tag")
        assert await _visible(mseed, tag_id=own.id, user=m, group_ids=frozenset({ga.id}))

    async def test_third_team_tag_not_visible(self, mseed: MultiGroupSeeder) -> None:
        ga, _la = await mseed.group_with_leader("Team A")
        gb, _lb = await mseed.group_with_leader("Team B")
        gc, _lc = await mseed.group_with_leader("Team C")
        m = await mseed.member(ga.id)
        await mseed.membership(user_id=m.id, group_id=gb.id)
        colleague_c = await mseed.member(gc.id)
        c_tag = await mseed.tag(user_id=colleague_c.id, name="c-team-tag")

        visible = await _visible(
            mseed, tag_id=c_tag.id, user=m, group_ids=frozenset({ga.id, gb.id})
        )
        assert visible is False, "a third team's tag must not be visible (404 in API)"
