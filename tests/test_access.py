"""Who can see and do what, once sign-in is on: invites, roles, access, characters, admin."""

import urllib.parse

from campaign.accounts import Accounts
from campaign.db import Store
from campaign.world import World

BOOK = [{"name": "Morgantha", "kind": "npc", "aliases": ["the windmill hag"], "summary": "A night hag.",
         "pages": [9], "facts": ["Sells dream pastries"], "stat_block": None}]


def invite_path(url):
    return urllib.parse.urlsplit(url).path


def setup_campaign(site):
    """A DM who owns a campaign with a DM-only module digested into the world."""
    dm = site.user("dm@example.com", admin=True)
    assert dm.post("/api/campaigns", json={"name": "Strahd", "role": "dm"}).json()["access"] == "owner"
    dm.post("/api/campaigns/strahd/sources", files={"file": ("cos.md", b"# Bonegrinder\nMorgantha is a night hag.")},
            data={"kind": "module"})
    s = Store(site.db)
    c = s.campaign("strahd")
    src = s.sources(c)[0]["id"]
    w = World(s, c)
    w.apply_book(src, BOOK, key="k")
    s.upsert_session(c, 1)
    w.record(w.resolve("Morgantha")["id"], "Morgantha bakes pies in the windmill", w.start_run("ingest", session=1), 1)
    s.close()
    return dm, src


def join(site, dm, who, role="player", access="viewer", **kw):
    r = dm.post("/api/campaigns/strahd/invites", json={"role": role, "access": access} | kw)
    assert r.status_code == 201, r.text
    path = invite_path(r.json()["url"])
    b = site.browser()
    first = b.get(path)
    assert first.status_code == 303 and first.headers["location"].startswith("/auth/login?invite=")
    token = urllib.parse.parse_qs(urllib.parse.urlsplit(first.headers["location"]).query)["invite"][0]
    landed = site.sign_in(b, who, invite=token)
    return b, landed, path


def test_invite_brings_a_new_person_into_one_campaign(site):
    dm, _ = setup_campaign(site)
    player, landed, path = join(site, dm, "pc@example.com")
    assert landed.status_code == 303 and landed.headers["location"] == "/#/campaigns/strahd"
    mine = player.get("/api/campaigns").json()
    assert [(c["slug"], c["role"], c["access"]) for c in mine] == [("strahd", "player", "viewer")]
    # The same person following their used link again just lands in the campaign.
    assert player.get(path).headers["location"] == "/#/campaigns/strahd"
    # Someone else can't reuse it.
    other = site.browser()
    assert other.get(path).status_code == 400 and "already been used" in other.get(path).text
    members = dm.get("/api/campaigns/strahd/members").json()["members"]
    assert [(m["email"], m["role"], m["access"]) for m in members] == [
        ("dm@example.com", "dm", "owner"), ("pc@example.com", "player", "viewer")]
    assert dm.get("/api/campaigns/strahd/invites").json() == []  # used up


def test_invites_can_be_tied_to_an_email_and_revoked(site):
    dm, _ = setup_campaign(site)
    r = dm.post("/api/campaigns/strahd/invites", json={"email": "pal@example.com"}).json()
    token = invite_path(r["url"]).rsplit("/", 1)[1]
    wrong = site.sign_in(site.browser(), "impostor@example.com", invite=token)
    assert wrong.status_code == 403 and "pal@example.com" in wrong.text
    _, ok, _ = join(site, dm, "pal@example.com", email="pal@example.com")
    assert ok.status_code == 303

    r = dm.post("/api/campaigns/strahd/invites", json={"max_uses": 5}).json()
    assert dm.delete(f"/api/campaigns/strahd/invites/{r['invite']['id']}").status_code == 204
    assert site.browser().get(invite_path(r["url"])).status_code == 400


def test_players_see_the_spoiler_filtered_world_and_cannot_change_it(site):
    dm, src = setup_campaign(site)
    player, _, _ = join(site, dm, "pc@example.com")
    dm_view = dm.get("/api/campaigns/strahd/entities").json()["entities"]
    assert [e["name"] for e in dm_view] == ["Morgantha"]
    ent = dm_view[0]["id"]
    detail = player.get(f"/api/campaigns/strahd/entities/{ent}").json()
    assert [f["statement"] for f in detail["facts"]] == ["Morgantha bakes pies in the windmill"]  # not "night hag"
    assert player.get("/api/campaigns/strahd/sources").json() == []  # DM-only module not even listed
    for path in (f"/api/campaigns/strahd/sources/{src}/outline", f"/api/campaigns/strahd/sources/{src}/original"):
        assert player.get(path).status_code == 404
    assert player.get("/api/campaigns/strahd/search", params={"q": "hag"}).json() == []
    # Viewers can't change anything.
    assert player.post(f"/api/campaigns/strahd/entities/{ent}/facts", json={"statement": "x"}).status_code == 403
    assert player.post("/api/campaigns/strahd/ingest", data={"text": "x"}).status_code == 403
    assert player.post("/api/campaigns/strahd/sessions/1/revert").status_code == 403
    assert player.post("/api/campaigns/strahd/invites", json={}).status_code == 403
    assert player.get("/api/campaigns/strahd/invites").status_code == 403


def test_player_editors_can_change_things_but_still_not_see_secrets(site):
    dm, src = setup_campaign(site)
    editor, _, _ = join(site, dm, "notes@example.com", access="editor")
    ent = dm.get("/api/campaigns/strahd/entities").json()["entities"][0]["id"]
    assert editor.post(f"/api/campaigns/strahd/entities/{ent}/facts", json={"statement": "Owes us 5 gp"}).status_code == 201
    assert [s["dm_only"] for s in editor.get("/api/campaigns/strahd/sources").json()] == [1]  # listed, to manage
    assert editor.get(f"/api/campaigns/strahd/sources/{src}/outline").status_code == 404  # but not readable
    assert editor.post("/api/campaigns/strahd/invites", json={}).status_code == 403  # owners only


def test_outsiders_cannot_tell_a_campaign_exists(site):
    setup_campaign(site)
    stranger = site.user("other@example.com")
    for path in ("/api/campaigns/strahd/entities", "/api/campaigns/strahd/sources", "/api/campaigns/strahd/members"):
        assert stranger.get(path).status_code == 404
    assert stranger.get("/api/campaigns").json() == []
    assert stranger.post("/api/characters", json={"name": "Spy", "campaign": "strahd"}).status_code == 404


def test_membership_management(site):
    dm, _ = setup_campaign(site)
    player, _, _ = join(site, dm, "pc@example.com")
    pid = player.get("/api/me").json()["id"]
    dm_id = dm.get("/api/me").json()["id"]
    assert dm.delete(f"/api/campaigns/strahd/members/{dm_id}").status_code == 409  # last owner
    assert player.delete(f"/api/campaigns/strahd/members/{dm_id}").status_code == 403
    r = dm.patch(f"/api/campaigns/strahd/members/{pid}", json={"role": "dm", "access": "editor"})
    assert ("pc@example.com", "dm", "editor") in [(m["email"], m["role"], m["access"]) for m in r.json()]
    assert player.delete(f"/api/campaigns/strahd/members/{pid}").status_code == 204  # leaving
    assert player.get("/api/campaigns/strahd/entities").status_code == 404


def test_character_sheets_belong_to_their_players(site):
    dm, _ = setup_campaign(site)
    alice, _, _ = join(site, dm, "alice@example.com")
    bob, _, _ = join(site, dm, "bob@example.com")
    sheet = alice.post("/api/characters", json={"name": "Ireth", "campaign": "strahd"}).json()
    cid = sheet["id"]
    assert sheet["can_edit"]
    # Party members can read it, but only its owner and the DM can change it.
    assert bob.get(f"/api/characters/{cid}").json()["can_edit"] is False
    assert bob.post(f"/api/characters/{cid}/hp", json={"delta": -3}).status_code == 403
    assert dm.post(f"/api/characters/{cid}/hp", json={"delta": -3}).status_code == 200
    assert alice.post(f"/api/characters/{cid}/rest", json={"long": True}).status_code == 200
    # A player sees their own character in the world, even before anything has happened to it.
    assert alice.get(f"/api/campaigns/strahd/entities/{sheet['entity_id']}").json()["kind"] == "pc"
    # A sheet in a campaign you're not in is invisible.
    assert bob.post("/api/campaigns", json={"name": "Side Game", "role": "dm"}).status_code == 201
    side = bob.post("/api/characters", json={"name": "Side character", "campaign": "side-game"}).json()["id"]
    assert alice.get(f"/api/characters/{side}").status_code == 404
    assert {c["name"] for c in alice.get("/api/characters").json()} == {"Ireth"}
    assert {c["name"] for c in bob.get("/api/characters").json()} == {"Ireth", "Side character"}
    # Copying: only into a campaign you're in, and only a sheet you could edit.
    assert alice.post(f"/api/characters/{cid}/copy", json={"campaign": "side-game"}).status_code == 404
    assert bob.post(f"/api/characters/{cid}/copy", json={"campaign": "side-game"}).status_code == 403
    assert alice.post("/api/campaigns", json={"name": "One Shot", "role": "player"}).status_code == 201
    copy = alice.post(f"/api/characters/{cid}/copy", json={"campaign": "one-shot"}).json()
    assert copy["copied_from"]["id"] == cid and copy["can_edit"]
    assert bob.get(f"/api/characters/{copy['id']}").status_code == 404


def test_the_party_stash_is_the_whole_partys(site):
    dm, _ = setup_campaign(site)
    alice, _, _ = join(site, dm, "alice@example.com")  # a player with viewer access
    bob, _, _ = join(site, dm, "bob@example.com")
    ireth = alice.post("/api/characters", json={"name": "Ireth", "campaign": "strahd"}).json()["id"]
    # Anyone in the party can put things in and change it, whatever their access.
    assert alice.post("/api/campaigns/strahd/stash/coin", json={"currency": {"gp": 30}}).status_code == 200
    log = bob.get("/api/campaigns/strahd/stash").json()["log"]
    assert log[0]["who"] == "alice@example.com"
    # Moving things in and out of a sheet needs the right to change that sheet.
    take = {"character": ireth, "to": "character", "currency": {"gp": 10}}
    assert bob.post("/api/campaigns/strahd/stash/move", json=take).status_code == 403
    assert alice.post("/api/campaigns/strahd/stash/move", json=take).status_code == 200
    assert dm.post("/api/campaigns/strahd/stash/move", json=take).status_code == 200
    assert [c["can_edit"] for c in bob.get("/api/campaigns/strahd/stash").json()["characters"]] == [False]
    # Outsiders can't see it.
    carol = site.user("carol@example.com")
    assert carol.get("/api/campaigns/strahd/stash").status_code == 404


def test_admin_pages(site):
    dm, _ = setup_campaign(site)
    player, _, _ = join(site, dm, "pc@example.com")
    assert player.get("/api/admin/users").status_code == 403
    assert player.get("/api/info").status_code == 403
    users = dm.get("/api/admin/users").json()
    assert {u["email"] for u in users} == {"dm@example.com", "pc@example.com"}
    pid = next(u["id"] for u in users if u["email"] == "pc@example.com")
    dm_id = dm.get("/api/me").json()["id"]
    assert dm.patch(f"/api/admin/users/{dm_id}", json={"is_admin": False}).status_code == 409
    assert dm.delete(f"/api/admin/users/{pid}").status_code == 204
    assert player.get("/api/campaigns").status_code == 401  # signed out immediately

    r = dm.post("/api/admin/invites", json={"days": 3}).json()  # app access, no campaign
    newbie = site.browser()
    token = invite_path(r["url"]).rsplit("/", 1)[1]
    assert site.sign_in(newbie, "new@example.com", invite=token).headers["location"] == "/"
    assert newbie.get("/api/campaigns").json() == []
    assert newbie.post("/api/campaigns", json={"name": "My Game", "role": "dm"}).status_code == 201


def test_cli_bootstrap_claims_unowned_campaigns(site):
    s = Store(site.db)
    s.create_campaign("Made Locally", "player")
    acc = Accounts(s)
    me = acc.create_user("me@example.com", is_admin=True)
    assert acc.claim_unowned(me["id"]) == ["made-locally"]
    assert acc.claim_unowned(me["id"]) == []
    s.close()
    b = site.browser()
    site.sign_in(b, "me@example.com")
    assert [(c["slug"], c["role"], c["access"]) for c in b.get("/api/campaigns").json()] == [
        ("made-locally", "player", "owner")]
