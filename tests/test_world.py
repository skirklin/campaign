"""The temporal world model: facts over sessions, history, sources, spoilers, undo."""

from fastapi.testclient import TestClient

from campaign.db import Store
from campaign.web import create_app
from campaign.world import World

BOOK = [
    {"name": "Ireena Kolyana", "kind": "npc", "aliases": ["Ireena"], "summary": "Adopted daughter of the burgomaster.",
     "pages": [3], "facts": ["Lives in the Village of Barovia", "Strahd has bitten her twice"], "stat_block": None},
    {"name": "Morgantha", "kind": "npc", "aliases": ["the windmill hag"], "summary": "A night hag who sells dream pastries.",
     "pages": [9], "facts": [], "stat_block": {"armor_class": "17", "hit_points": "112", "speed": "30 ft.", "strength": 18,
     "dexterity": 15, "constitution": 16, "intelligence": 16, "wisdom": 14, "charisma": 16, "challenge": "5",
     "traits": [], "actions": [], "other": []}},
]


def setup(role="dm", dm_only=True):
    store = Store(":memory:")
    camp = store.create_campaign("Strahd", role)
    src = store.add_source(camp, "Curse of Strahd", "module", [{"heading": "", "text": "x", "page": 1}], dm_only=dm_only)
    w = World(store, camp)
    w.apply_book(src, BOOK, key="v1")
    return store, camp, w, src


def play(store, w, number):
    store.upsert_session(w.campaign, number)
    return w.start_run("ingest", session=number)


def test_attributes_supersede_and_history_is_queryable():
    store, _, w, _ = setup()
    ireena = w.resolve("ireena")["id"]
    w.record(ireena, "Ireena is in Vallaki", play(store, w, 2), 2, attribute="location", value="Vallaki")
    w.record(ireena, "Ireena is at the Abbey", play(store, w, 5), 5, attribute="location", value="Abbey")
    assert w.view(ireena)["current"] == {"location": "Abbey"}
    assert w.view(ireena, as_of=4)["current"] == {"location": "Vallaki"}
    assert w.view(ireena, as_of=1)["current"] == {}
    assert [p["statement"] for p in w.view(ireena)["past"]] == ["Ireena is in Vallaki"]
    assert w.view(ireena)["past"][0]["sessions"] == "2-5"
    # as of session 4, the move to the Abbey hadn't happened, so Vallaki isn't "past" yet
    assert w.view(ireena, as_of=4)["past"] == []
    changes = [(t["session"], t["change"]) for t in w.timeline(ireena) if t.get("attribute") == "location"]
    assert changes == [(2, "began"), (5, "ended"), (5, "began")]


def test_book_is_the_starting_state_and_play_supersedes_it():
    store, _, w, _ = setup()
    ireena = w.resolve("Ireena Kolyana")["id"]
    lives = next(f for f in w.facts(ireena) if f["statement"].startswith("Lives"))
    assert (lives["valid_from"], lives["source"], lives["pages"]) == (0, "Curse of Strahd", [3])
    w.end_fact(lives["id"], play(store, w, 3), 3)
    view = w.view(ireena)
    assert "Lives in the Village of Barovia" in [p["statement"] for p in view["past"]]
    assert "Lives in the Village of Barovia" in [f["statement"] for f in w.view(ireena, as_of=2)["facts"]]
    hag = w.lookup("the windmill hag")[0]
    assert hag["name"] == "Morgantha" and "stat_block" not in hag["current"]


def test_revert_undoes_exactly_one_session():
    store, _, w, _ = setup()
    ireena = w.resolve("Ireena")["id"]
    r2 = play(store, w, 2)
    w.record(ireena, "Ireena is in Vallaki", r2, 2, attribute="location", value="Vallaki")
    w.add_event("Escorted Ireena to Vallaki", 2, r2, [ireena])
    r3 = play(store, w, 3)
    w.record(ireena, "Ireena was kidnapped", r3, 3, attribute="location", value="unknown")
    lives = next(f for f in w.facts(ireena) if f["statement"].startswith("Lives"))
    w.end_fact(lives["id"], r3, 3)
    villain = w.ensure_entity("Izek Strazni", "npc")
    w.record(villain["id"], "Izek took her", r3, 3)

    assert w.revert_session(3) == {"facts": 2, "reopened": 2, "events": 0, "entities_removed": 1}
    assert w.view(ireena)["current"] == {"location": "Vallaki"}
    assert lives["statement"] in [f["statement"] for f in w.view(ireena)["facts"]]
    assert w.resolve("Izek Strazni") is None
    assert len(w.events()) == 1


def test_reapplying_the_book_is_idempotent_and_keeps_table_history():
    store, _, w, src = setup()
    ireena = w.resolve("Ireena")["id"]
    lives = next(f for f in w.facts(ireena) if f["statement"].startswith("Lives"))
    run = play(store, w, 4)
    w.end_fact(lives["id"], run, 4)
    w.record(ireena, "Ireena trusts the party", run, 4, attribute="disposition", value="trusting")
    assert w.apply_book(src, BOOK, key="v1") is None  # same digest: nothing to do

    revised = [dict(BOOK[0], summary="Ireena, a young woman of Barovia."), BOOK[1]]
    assert w.apply_book(src, revised, key="v2")
    assert w.resolve("Ireena")["id"] == ireena  # same identity, so the table's facts are still attached
    lives = next(f for f in w.facts(ireena) if f["statement"].startswith("Lives"))
    assert lives["valid_to"] == 4  # the table's ending carried over to the re-applied fact
    assert w.view(ireena)["current"] == {"disposition": "trusting"}
    assert "Ireena, a young woman of Barovia." in [f["statement"] for f in w.view(ireena)["facts"]]


def test_spoilers_are_per_fact():
    store, _, w, _ = setup(role="player", dm_only=False)  # the party's own notes: known from the start
    ireena = w.resolve("Ireena")["id"]
    run = play(store, w, 2)
    w.record(ireena, "Ireena is a reincarnation of Tatyana", run, 2, party_knows=False)
    assert "Ireena is a reincarnation of Tatyana" not in [f["statement"] for f in w.view(ireena)["facts"]]
    secret = w.store.conn.execute("SELECT id FROM facts WHERE statement LIKE '%Tatyana%'").fetchone()["id"]
    play(store, w, 6)
    assert w.party_learned(secret, 6)
    assert "Ireena is a reincarnation of Tatyana" in [f["statement"] for f in w.view(ireena)["facts"]]
    assert "Ireena is a reincarnation of Tatyana" not in [f["statement"] for f in w.view(ireena, as_of=5)["facts"]]

    _, _, hidden, _ = setup(role="player", dm_only=True)  # a DM-only module: nothing visible
    assert hidden.list() == [] and hidden.lookup("Ireena") == []


def test_merge_combines_identities_and_resolves_conflicts():
    store, _, w, _ = setup()
    run = play(store, w, 2)
    baker = w.ensure_entity("the old baker woman", "npc")
    w.record(baker["id"], "Sells pies at the windmill", run, 2, attribute="location", value="windmill")
    hag = w.resolve("Morgantha")
    w.record(hag["id"], "Seen near the Tser Pool", play(store, w, 3), 3, attribute="location", value="Tser Pool")
    merged = w.merge(hag["id"], baker["id"])
    assert "the old baker woman" in merged["aliases"]
    view = w.view(hag["id"])
    assert view["current"] == {"location": "Tser Pool"}  # the newer value wins
    assert w.resolve("the old baker woman")["id"] == hag["id"]


def test_web_world_endpoints(tmp_path):
    client = TestClient(create_app(tmp_path / "w.db"))
    client.post("/api/campaigns", json={"name": "Strahd", "role": "dm"})
    s = Store(tmp_path / "w.db")
    camp = s.campaign("strahd")
    src = s.add_source(camp, "CoS", "module", [{"heading": "", "text": "x", "page": 1}])
    World(s, camp).apply_book(src, BOOK, key="v1")
    s.close()

    listing = client.get("/api/campaigns/strahd/entities").json()
    assert listing["now"] == 0 and {e["name"] for e in listing["entities"]} == {"Ireena Kolyana", "Morgantha"}
    assert client.get("/api/campaigns/strahd/entities", params={"in_play": "true"}).json()["entities"] == []
    hag = next(e for e in listing["entities"] if e["name"] == "Morgantha")

    r = client.post(f"/api/campaigns/strahd/entities/{hag['id']}/facts",
                    json={"statement": "Morgantha is dead", "attribute": "status", "value": "dead"})
    assert r.status_code == 201
    detail = client.get(f"/api/campaigns/strahd/entities/{hag['id']}").json()
    assert detail["current"] == {"status": "dead"}
    assert {t["change"] for t in detail["timeline"]} == {"began"}
    fact_id = next(f["id"] for f in detail["all_facts"] if f["attribute"] == "status")
    assert client.post(f"/api/campaigns/strahd/facts/{fact_id}/end").json() == {"ended": True}
    assert client.post(f"/api/campaigns/strahd/facts/{fact_id}/end").status_code == 404

    ireena = next(e for e in listing["entities"] if e["name"] == "Ireena Kolyana")
    merged = client.post(f"/api/campaigns/strahd/entities/{hag['id']}/merge", json={"other_id": ireena["id"]}).json()
    assert "Ireena Kolyana" in merged["aliases"]
