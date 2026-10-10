from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from domain_test_support import as_api_role, domain_database
from test_api_db_public_ops import seed_profile
from test_api_db_verification import (
    call_support_transfer,
    support_connection,
    verification_binding,
)

from clashlens import api_accounts, api_crews, api_verification
from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.verification import VerificationOutcome

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class Site:
    """Clash Lens accounts and their linked game accounts, with crew calls
    made as the production API role."""

    def __init__(self, connection_info: str) -> None:
        self.info = connection_info
        self.seed = ApiDatabase(connection_info)
        self.api = ApiDatabase(as_api_role(connection_info), max_size=8)
        self.ids: dict[str, int] = {}

    def close(self) -> None:
        self.api.close()
        self.seed.close()

    def account(self, username: str) -> str:
        created = api_accounts.create_account(
            self.api,
            RequestBinding(
                request_id=str(uuid4()),
                caller="typescript-website",
                provider="google",
                provider_subject=f"subject-{username}",
                account_id=None,
                operation="account.create",
                method="POST",
                request_target="/v1/account",
                identity={"username": username},
            ),
            username=username,
            normalized_username=username,
            display_name=username.title(),
        )
        assert created.status_code == 201
        context = api_accounts.resolve_account(self.api, "google", f"subject-{username}")
        assert context is not None
        self.ids[username] = context.internal_id
        return username

    def link(self, username: str, tag: str, state: str = "tracking") -> str:
        """Link a game account: tracked in Legend League, out of it, or not
        checked yet."""
        known = self.sql("SELECT 1 FROM players WHERE normalized_tag = %s", (tag,))
        if state != "unchecked" and not known:
            seed_profile(self.seed, tag, 5000)
        binding = verification_binding(self.ids[username], f"subject-{username}", tag)
        assert api_verification.reserve_verification(
            self.api, binding, normalized_tag=tag
        ).fresh
        linked = api_verification.complete_verification(
            self.api,
            binding,
            normalized_tag=tag,
            outcome=VerificationOutcome.VERIFIED,
            account_id=self.ids[username],
            # A support transfer must follow its fresh verification within
            # 15 minutes of the database clock.
            completed_at=datetime.now(UTC),
        )
        assert linked.payload["status"] in {"linked", "support_required"}
        if state == "not_in_legend":
            self.sql(
                """
                UPDATE players SET active = false, eligibility_state = 'ineligible'
                WHERE normalized_tag = %s
                """,
                (tag,),
            )
        return linked.payload.get("verification_request_id")

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.seed.pool.connection() as connection:
            cursor = connection.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def call(self, caller: str, function: Any, /, **values: Any) -> Any:
        binding = RequestBinding(
            request_id=values.pop("request_id", None) or str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject=f"subject-{caller}",
            account_id=self.ids[caller],
            operation=f"crews.{function.__name__}",
            method="POST",
            request_target="/v1/account/crews",
            identity={key: str(value) for key, value in values.items()},
        )
        return function(self.api, binding, **values)

    def crew(self, username: str, crew_id: str, now: datetime = NOW) -> Any:
        return api_crews.get_crew(self.api, self.ids[username], crew_id, now=now)

    def make(self, username: str, tags: list[str], size: int = 50) -> str:
        created = self.call(
            username, api_crews.create_crew, name="Night Owls", size=size, tags=tags
        )
        assert created.status_code == 201, created.payload
        return created.payload["crew_id"]

    def invite(self, username: str, crew_id: str, *, new: bool = False, now=NOW) -> str:
        made = self.call(username, api_crews.make_invite, crew_id=crew_id, new=new, now=now)
        assert made.status_code == 200, made.payload
        return made.payload["code"]

    def accept(self, username: str, code: str, tags: list[str], now=NOW) -> Any:
        return self.call(username, api_crews.accept_invite, code=code, tags=tags, now=now)


@pytest.fixture
def site(database_url: str):
    with domain_database(database_url) as connection_info:
        built = Site(connection_info)
        try:
            yield built
        finally:
            built.close()


def test_creating_and_listing_crews(site: Site) -> None:
    site.account("akira")
    site.link("akira", "#2PP")
    site.link("akira", "#8PY")
    crew_id = site.make("akira", ["#8PY", "#2PP"], size=10)

    listed = api_crews.list_crews(site.api, site.ids["akira"])
    assert listed == {
        "kind": "crew-list",
        "crews": [
            {
                "crew_id": crew_id,
                "name": "Night Owls",
                "size": 10,
                "used": 2,
                "role": "owner",
                "my_tags": ["#2PP", "#8PY"],
            }
        ],
        "crew_count": 1,
        "max_crews": 5,
    }
    crew = site.crew("akira", crew_id)
    assert (crew["my_role"], crew["used"], crew["invites"]) == ("owner", 2, [])
    [member] = crew["members"]
    assert (member["username"], member["role"], member["you"]) == ("akira", "owner", True)
    assert [(player["tag"], player["status"]) for player in member["players"]] == [
        ("#2PP", "tracking"),
        ("#8PY", "tracking"),
    ]
    # Repeating the same request gives the stored answer and no second crew.
    request_id = str(uuid4())
    first = site.call(
        "akira", api_crews.create_crew, request_id=request_id,
        name="Twice", size=5, tags=["#2PP"],
    )
    again = site.call(
        "akira", api_crews.create_crew, request_id=request_id,
        name="Twice", size=5, tags=["#2PP"],
    )
    assert again.payload == first.payload and again.replayed
    assert api_crews.list_crews(site.api, site.ids["akira"])["crew_count"] == 2


def test_create_refuses_accounts_that_cannot_join(site: Site) -> None:
    site.account("akira")
    site.account("bea")
    site.link("akira", "#2PP")
    site.link("akira", "#8PY", "not_in_legend")
    site.link("akira", "#9QQ", "unchecked")
    site.link("bea", "#PPP")

    def refused(tags: list[str]) -> tuple[int, dict[str, Any]]:
        result = site.call(
            "akira", api_crews.create_crew, name="Nope", size=5, tags=tags
        )
        return result.status_code, result.payload

    assert refused(["#PPP"]) == (422, {"error": "player_not_linked", "tag": "#PPP"})
    assert refused(["#2PP", "#8PY"]) == (
        422, {"error": "player_not_in_legend", "tag": "#8PY"},
    )
    status, payload = refused(["#9QQ"])
    assert (status, payload["error"], payload["tag"]) == (409, "player_not_checked", "#9QQ")
    assert refused(["#2PP", "#8PY", "#9QQ"])[0] == 422
    # A refused create leaves nothing behind.
    assert site.sql("SELECT count(*) FROM crews") == [(0,)]


def test_joining_by_invite_link(site: Site) -> None:
    for name in ("owner", "joiner"):
        site.account(name)
    site.link("owner", "#2PP")
    site.link("joiner", "#8PY")
    site.link("joiner", "#9QQ")
    site.link("joiner", "#PPP", "not_in_legend")
    crew_id = site.make("owner", ["#2PP"], size=3)
    code = site.invite("owner", crew_id)

    preview = api_crews.get_invite(site.api, site.ids["joiner"], code, now=NOW)
    assert {key: preview[key] for key in ("state", "name", "owner_display_name", "used", "size", "in_crew")} == {
        "state": "ok",
        "name": "Night Owls",
        "owner_display_name": "Owner",
        "used": 1,
        "size": 3,
        "in_crew": False,
    }
    assert [(account["tag"], account["eligibility"]) for account in preview["accounts"]] == [
        ("#8PY", "ok"),
        ("#9QQ", "ok"),
        ("#PPP", "not_in_legend"),
    ]
    assert site.accept("joiner", code, ["#PPP"]).payload["error"] == "player_not_in_legend"
    joined = site.accept("joiner", code, ["#8PY"])
    assert joined.payload == {"crew_id": crew_id}
    preview = api_crews.get_invite(site.api, site.ids["joiner"], code, now=NOW)
    assert preview["in_crew"] is True
    assert [account["eligibility"] for account in preview["accounts"]][:2] == [
        "already_in_crew",
        "ok",
    ]
    assert site.accept("joiner", code, ["#8PY"]).payload == {
        "error": "player_already_in_crew",
        "tag": "#8PY",
    }
    # A member can add another of their accounts through the same link.
    assert site.accept("joiner", code, ["#9QQ"]).status_code == 200
    # Now full: the link still opens and says so, and joining is refused.
    site.account("late")
    site.link("late", "#QQQ")
    assert api_crews.get_invite(site.api, site.ids["late"], code, now=NOW)["state"] == "full"
    assert site.accept("late", code, ["#QQQ"]).payload == {
        "error": "crew_full",
        "open_places": 0,
    }
    assert site.call(
        "owner", api_crews.make_invite, crew_id=crew_id, new=True, now=NOW
    ).payload == {"error": "crew_full", "open_places": 0}


def test_links_that_no_longer_work_read_alike(site: Site) -> None:
    for name in ("owner", "joiner"):
        site.account(name)
    site.link("owner", "#2PP")
    site.link("joiner", "#8PY")
    crew_id = site.make("owner", ["#2PP"])
    expired = site.invite("owner", crew_id)
    turned_off = site.invite("owner", crew_id, new=True)
    invite_id = site.sql(
        "SELECT public_id::text FROM crew_invites WHERE code = %s", (turned_off,)
    )[0][0]
    assert site.call(
        "owner", api_crews.revoke_invite, crew_id=crew_id, invite_id=invite_id, now=NOW
    ).payload == {"revoked": True, "invite_id": invite_id}
    later = NOW + timedelta(hours=48)
    unknown = "A" * 22
    for code, now in ((expired, later), (turned_off, NOW), (unknown, NOW)):
        assert api_crews.get_invite(site.api, site.ids["joiner"], code, now=now) == {
            "kind": "crew-invite",
            "state": "invalid",
            "in_crew": False,
            "crew_count": 0,
            "accounts": [],
        }
        assert site.accept("joiner", code, ["#8PY"], now=now).payload == {
            "error": "invite_invalid"
        }
    # Deleting the crew turns its links off too.
    live = site.invite("owner", crew_id)
    assert site.call("owner", api_crews.delete_crew, crew_id=crew_id).status_code == 200
    assert site.accept("joiner", live, ["#8PY"]).payload == {"error": "invite_invalid"}


def test_invite_links_are_reused_then_replaced(site: Site) -> None:
    site.account("owner")
    site.link("owner", "#2PP")
    crew_id = site.make("owner", ["#2PP"])
    first = site.invite("owner", crew_id)
    assert site.invite("owner", crew_id, now=NOW + timedelta(hours=24)) == first
    # Under a day left: a fresh link instead.
    second = site.invite("owner", crew_id, now=NOW + timedelta(hours=25))
    assert second != first
    codes = [first, second]
    for hour in (26, 27):
        codes.append(site.invite("owner", crew_id, new=True, now=NOW + timedelta(hours=hour)))
    # Making a fourth live link turned the oldest off.
    later = NOW + timedelta(hours=28)
    live = site.sql(
        """
        SELECT code FROM crew_invites
        WHERE revoked_at IS NULL AND expires_at > %s ORDER BY created_at
        """,
        (later,),
    )
    assert [row[0] for row in live] == codes[1:]
    # Expired links are deleted when the crew makes a new one.
    site.invite("owner", crew_id, new=True, now=NOW + timedelta(hours=49))
    assert site.sql(
        "SELECT count(*) FROM crew_invites WHERE code = %s", (first,)
    ) == [(0,)]


def test_five_crews_at_most_counting_every_role(site: Site) -> None:
    site.account("busy")
    site.account("host")
    site.link("host", "#2PP")
    tags = ["#8PY", "#9QQ", "#PPP", "#QQQ", "#YYY", "#LLL"]
    for tag in tags:
        site.link("busy", tag)
    for tag in tags[:4]:
        site.make("busy", [tag])
    hosted = site.make("host", ["#2PP"])
    code = site.invite("host", hosted)
    assert site.accept("busy", code, [tags[4]]).status_code == 200
    preview = api_crews.get_invite(site.api, site.ids["busy"], code, now=NOW)
    assert (preview["state"], preview["crew_count"]) == ("ok", 5)
    refused = site.call(
        "busy", api_crews.create_crew, name="Sixth", size=5, tags=[tags[5]]
    )
    assert refused.payload == {"error": "crew_limit_reached"}
    other = site.make("host", ["#2PP"])
    other_code = site.invite("host", other)
    assert api_crews.get_invite(site.api, site.ids["busy"], other_code, now=NOW)["state"] == "limit"
    assert site.accept("busy", other_code, [tags[5]]).payload == {
        "error": "crew_limit_reached"
    }


def test_two_joins_racing_for_the_last_place(site: Site) -> None:
    site.account("owner")
    site.link("owner", "#2PP")
    crew_id = site.make("owner", ["#2PP"], size=2)
    code = site.invite("owner", crew_id)
    for name, tag in (("left", "#8PY"), ("right", "#9QQ")):
        site.account(name)
        site.link(name, tag)
    start = Barrier(2)

    def join(name_tag: tuple[str, str]) -> int:
        start.wait()
        return site.accept(name_tag[0], code, [name_tag[1]]).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(join, [("left", "#8PY"), ("right", "#9QQ")]))
    assert results == [200, 409]
    assert site.sql("SELECT count(*) FROM crew_players") == [(2,)]


def test_size_cannot_go_below_the_places_in_use(site: Site) -> None:
    site.account("owner")
    site.link("owner", "#2PP")
    site.link("owner", "#8PY")
    site.link("owner", "#9QQ")
    crew_id = site.make("owner", ["#2PP", "#8PY", "#9QQ"], size=5)

    def resize(size: int) -> Any:
        return site.call("owner", api_crews.update_crew, crew_id=crew_id, size=size)

    assert resize(2).payload == {"error": "crew_size_below_used", "used": 3}
    assert resize(3).payload == {
        "crew_id": crew_id, "name": "Night Owls", "size": 3, "used": 3,
    }
    renamed = site.call("owner", api_crews.update_crew, crew_id=crew_id, name="Day Owls")
    assert renamed.payload["name"] == "Day Owls"


def test_roles_decide_who_can_kick_rename_and_hand_over(site: Site) -> None:
    for name, tags in (
        ("owner", ["#2PP"]),
        ("admin", ["#8PY"]),
        ("member", ["#9QQ", "#PPP"]),
        ("other", ["#QQQ"]),
    ):
        site.account(name)
        for tag in tags:
            site.link(name, tag)
    crew_id = site.make("owner", ["#2PP"])
    code = site.invite("owner", crew_id)
    for name, tags in (("admin", ["#8PY"]), ("member", ["#9QQ", "#PPP"]), ("other", ["#QQQ"])):
        assert site.accept(name, code, tags).status_code == 200

    def call(name: str, function: Any, /, **values: Any) -> tuple[int, Any]:
        result = site.call(name, function, crew_id=crew_id, **values)
        return result.status_code, result.payload.get("error")

    forbidden = (403, "crew_forbidden")
    assert call("member", api_crews.set_member_role, username="admin", role="member") == forbidden
    assert call("admin", api_crews.set_member_role, username="member", role="admin") == forbidden
    assert call("owner", api_crews.set_member_role, username="admin", role="admin") == (200, None)
    assert call("owner", api_crews.set_member_role, username="owner", role="member") == (
        409, "owner_role_fixed",
    )
    assert call("owner", api_crews.set_member_role, username="nobody", role="admin") == (
        404, "member_not_found",
    )
    # Only owners and admins rename; a member can't kick anyone.
    assert call("member", api_crews.update_crew, name="Mine") == forbidden
    assert call("admin", api_crews.update_crew, name="Admins Rule") == (200, None)
    assert call("member", api_crews.remove_player, tag="#QQQ") == forbidden
    # An admin kicks members' accounts, not the owner's or another admin's.
    assert call("admin", api_crews.remove_player, tag="#2PP") == forbidden
    kicked = site.call("admin", api_crews.remove_player, crew_id=crew_id, tag="#9QQ")
    assert kicked.payload == {"removed": True, "tag": "#9QQ", "left_crew": False}
    # The member's last place going takes the member out of the crew.
    kicked = site.call("admin", api_crews.remove_player, crew_id=crew_id, tag="#PPP")
    assert kicked.payload["left_crew"] is True
    assert site.crew("member", crew_id) is None
    assert call("owner", api_crews.remove_player, tag="#PPP") == (404, "player_not_in_crew")
    # A kick is not a ban.
    assert site.accept("member", code, ["#9QQ"]).status_code == 200
    # The owner can kick an admin's account.
    assert call("owner", api_crews.remove_player, tag="#8PY") == (200, None)
    assert site.crew("admin", crew_id) is None

    # The owner can't leave or drop their last account; they hand over first.
    assert call("owner", api_crews.leave_crew) == (409, "owner_must_hand_over")
    assert call("owner", api_crews.remove_player, tag="#2PP") == (409, "owner_must_hand_over")
    assert call("other", api_crews.hand_over, username="member") == forbidden
    assert call("owner", api_crews.hand_over, username="other") == (200, None)
    roles = site.sql(
        """
        SELECT account.normalized_username, member.role
        FROM crew_accounts AS member
        JOIN clash_lens_accounts AS account ON account.id = member.account_id
        ORDER BY 1
        """
    )
    assert roles == [("member", "member"), ("other", "owner"), ("owner", "admin")]
    assert call("owner", api_crews.delete_crew) == forbidden
    # Leaving takes every place with it.
    assert call("member", api_crews.leave_crew) == (200, None)
    assert site.sql(
        "SELECT count(*) FROM crew_players AS place JOIN players ON players.id = place.player_id WHERE normalized_tag = '#9QQ'"
    ) == [(0,)]
    assert call("other", api_crews.delete_crew) == (200, None)
    assert site.sql("SELECT count(*) FROM crew_accounts") == [(0,)]


def test_members_see_their_own_links_and_owners_see_all(site: Site) -> None:
    for name, tag in (("owner", "#2PP"), ("member", "#8PY"), ("stranger", "#9QQ")):
        site.account(name)
        site.link(name, tag)
    crew_id = site.make("owner", ["#2PP"])
    code = site.invite("owner", crew_id)
    assert site.accept("member", code, ["#8PY"]).status_code == 200
    site.invite("member", crew_id)
    assert [
        (invite["made_by"], invite["mine"])
        for invite in site.crew("member", crew_id)["invites"]
    ] == [("Member", True)]
    assert sorted(
        (invite["made_by"], invite["mine"])
        for invite in site.crew("owner", crew_id)["invites"]
    ) == [("Member", False), ("Owner", True)]
    # A member can't turn off someone else's link.
    owner_invite = site.sql(
        "SELECT public_id::text FROM crew_invites WHERE code = %s", (code,)
    )[0][0]
    assert site.call(
        "member", api_crews.revoke_invite, crew_id=crew_id, invite_id=owner_invite, now=NOW
    ).payload == {"error": "crew_forbidden"}

    # Someone outside the crew reads it as missing, for reads and writes.
    assert site.crew("stranger", crew_id) is None
    for function, values in (
        (api_crews.update_crew, {"name": "Taken"}),
        (api_crews.add_players, {"tags": ["#9QQ"]}),
        (api_crews.make_invite, {"new": False, "now": NOW}),
        (api_crews.leave_crew, {}),
        (api_crews.delete_crew, {}),
    ):
        result = site.call("stranger", function, crew_id=crew_id, **values)
        assert result.payload == {"error": "crew_not_found"}


def test_support_transfer_takes_the_account_out_of_its_crews(site: Site) -> None:
    for name in ("first", "second", "host"):
        site.account(name)
    site.link("first", "#2PP")
    site.link("first", "#8PY")
    site.link("host", "#9QQ")
    hosted = site.make("host", ["#9QQ"])
    assert site.accept("first", site.invite("host", hosted), ["#2PP"]).status_code == 200
    own = site.make("first", ["#2PP", "#8PY"])
    candidate = site.link("second", "#2PP")
    assert candidate is not None
    public = dict(
        site.sql("SELECT normalized_username, public_id::text FROM clash_lens_accounts")
    )
    with support_connection(site.info) as support:
        assert call_support_transfer(
            support,
            verification_request_id=candidate,
            player_tag="#2PP",
            from_account_public_id=public["first"],
            to_account_public_id=public["second"],
            operator_identity="sudo:operator:1000",
            reason="Fresh verification was reviewed.",
        ) == ("transferred", "#2PP")
    # Out of the hosted crew entirely, since that was its only place there;
    # still owner of its own crew, with the account it kept.
    assert site.crew("first", hosted) is None
    kept = site.crew("first", own)
    assert (kept["my_role"], kept["used"]) == ("owner", 1)
    assert api_crews.list_crews(site.api, site.ids["first"])["crews"][0]["my_tags"] == ["#8PY"]
    assert site.sql(
        "SELECT count(*) FROM crew_players AS place JOIN players ON players.id = place.player_id WHERE normalized_tag = '#2PP'"
    ) == [(0,)]
