"""The temporal world model: facts over sessions, history, sources, spoilers, undo."""

import pytest

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


def test_revert_undoes_exactly_one_upload():
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

    assert w.revert_upload(r3) == {"facts": 2, "reopened": 2, "events": 0, "entities_removed": 1,
                                   "session": 3, "session_removed": True}
    assert w.view(ireena)["current"] == {"location": "Vallaki"}
    assert lives["statement"] in [f["statement"] for f in w.view(ireena)["facts"]]
    assert w.resolve("Izek Strazni") is None
    assert len(w.events()) == 1
    assert [x["number"] for x in store.sessions(w.campaign)] == [2]  # nothing else was in session 3
    with pytest.raises(KeyError):
        w.revert_upload(r3)


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


def test_aliases_from_dm_only_material_are_spoilers_too():
    store, camp, w, src = setup(role="dm", dm_only=True)
    hag = w.resolve("the windmill hag")["id"]
    w.record(hag, "Morgantha sells pies to the party", play(store, w, 1), 1)
    from dataclasses import replace

    player = World(store, replace(camp, role="player"))
    view = player.view(hag)
    assert view["aliases"] == [] and view["name"] == "Morgantha"  # the party knows her name, not what she is
    assert player.resolve("the windmill hag") is None
    assert player.lookup("the windmill hag") == []
    assert [e["aliases"] for e in player.list()] == [[]]
    # Applying the book still matches its own aliases, so identities stay stable.
    assert w.apply_book(src, BOOK, key="v2") and w.resolve("the windmill hag")["id"] == hag


def test_a_name_is_a_secret_while_the_party_doesnt_know_it():
    """The leak a DM found: an alias written in play that gave a secret identity away."""
    from dataclasses import replace

    from campaign.tools import ToolContext, build_tools

    store, camp, w, _ = setup(role="dm", dm_only=False)
    store.upsert_session(camp, 2)
    dm = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    player = World(store, replace(camp, role="player"))
    dm["record_fact"]("Vasili von Holtz", "Vasili von Holtz is a nobleman visiting Vallaki.", kind="npc",
                      aliases=["the stranger"])
    dm["record_fact"]("Vasili von Holtz", "Vasili von Holtz is Strahd in disguise.", aliases=["Strahd"],
                      party_knows=False)
    vasili = w.resolve("Vasili von Holtz")["id"]
    assert player.view(vasili)["aliases"] == ["the stranger"]  # the secret name goes with the secret fact
    assert player.resolve("Strahd") is None and "Vasili von Holtz" not in [e["name"] for e in player.list(q="Strahd")]
    assert w.view(vasili)["secret_aliases"] == ["Strahd"]  # the DM sees which names are secret
    assert "secret_aliases" not in player.view(vasili)

    # One written as known by mistake is hidden, learned later, or taken away.
    dm["set_alias"]("Vasili von Holtz", "the vampire")
    assert "the vampire" in player.view(vasili)["aliases"]
    assert "doesn't know" in dm["set_alias"]("Vasili von Holtz", "the vampire", party_knows=False)
    assert "the vampire" not in player.view(vasili)["aliases"]
    store.upsert_session(camp, 3)
    assert "from session 3" in dm["set_alias"]("the stranger", "Strahd", party_knows=True)  # they found out
    assert "Strahd" in player.view(vasili)["aliases"]
    assert "no longer" in dm["remove_alias"]("Vasili von Holtz", "the vampire")
    assert w.resolve("the vampire") is None
    assert "has no alias" in dm["remove_alias"]("Vasili von Holtz", "the vampire")
    assert "own name" in dm["set_alias"]("the stranger", "Vasili von Holtz")

    # A known fact that says too much is hidden, or reworded; the book's can be hidden but not reworded.
    fact = next(f for f in w.facts(vasili) if "nobleman" in f["statement"])
    assert dm["edit_fact"](fact["id"], statement="Vasili von Holtz is a nobleman staying in Vallaki.") \
        == f"Fact {fact['id']} rewritten."
    assert [v["name"] for v in w.lookup("staying")] == ["Vasili von Holtz"]  # the search follows the words
    assert dm["keep_from_party"](fact["id"]) == f"Fact {fact['id']} is kept from the party."
    assert fact["id"] not in [f["id"] for f in player.facts(vasili)]
    lives = next(f for f in w.facts(w.resolve("Ireena")["id"]) if f["statement"].startswith("Lives"))
    assert "can't be reworded" in dm["edit_fact"](lives["id"], statement="x")
    assert dm["keep_from_party"](lives["id"]).endswith("kept from the party.")


def test_what_the_party_knows_of_the_books_names_outlasts_digesting_it_again():
    from dataclasses import replace

    store, camp, w, src = setup(role="dm", dm_only=False)
    player = World(store, replace(camp, role="player"))
    hag = w.resolve("Morgantha")["id"]
    assert player.view(hag)["aliases"] == ["the windmill hag"]  # the party's own notes: known from the start
    w.alias_known(hag, "the windmill hag", None)
    assert w.apply_book(src, BOOK, key="v2")
    assert player.view(hag)["aliases"] == []  # still kept from them
    store.set_source_dm_only(src, True)
    store.set_source_dm_only(src, False)
    assert player.view(hag)["aliases"] == ["the windmill hag"]  # a visible book's names are known
    store.set_source_dm_only(src, True)
    assert player.view(hag) is None or player.view(hag)["aliases"] == []


def test_whatever_play_wrote_can_be_renamed_rewritten_or_deleted():
    from campaign.tools import ToolContext, build_tools

    store, camp, w, _ = setup(role="dm", dm_only=False)
    # Written by a session's ingest, not in chat: still the DM's to fix.
    run = play(store, w, 1)
    stranger = w.ensure_entity("Strahd's disguise", "npc")["id"]
    w.record(stranger, "A stranger in a black cloak watched the party.", run, 1)
    event = w.add_event("Strahd, in disguise, watched the party.", 1, run, [stranger])
    w.record(w.resolve("Ireena")["id"], "Ireena told the party her secret.", run, 1)
    store.upsert_session(camp, 1, title="Strahd watches", summary="Strahd, disguised, watched them.")
    dm = {t.__name__: t for t in build_tools(ToolContext(store, camp))}

    assert dm["rename_entity"]("Strahd's disguise", "The cloaked stranger") == \
        "Renamed Strahd's disguise to The cloaked stranger."
    assert w.resolve("Strahd's disguise") is None and w.entity(stranger)["aliases"] == []
    assert "already called" in dm["rename_entity"]("The cloaked stranger", "Morgantha")
    assert dm["edit_journal_entry"](event, "A cloaked stranger watched the party.") == f"Journal entry {event} rewritten."
    assert w.events()[0]["text"] == "A cloaked stranger watched the party."
    assert dm["edit_session"](1, title="Watched", summary="A cloaked stranger watched them.") == "Session 1 rewritten."
    assert [(x["title"], x["summary"]) for x in store.sessions(camp)] == [("Watched", "A cloaked stranger watched them.")]
    told = next(f for f in w.facts(w.resolve("Ireena")["id"]) if "secret" in f["statement"])
    assert dm["retract_fact"](told["id"]) == f"Deleted fact {told['id']}."

    assert "can't be deleted: the reference material" in dm["delete_entity"]("Morgantha")
    assert dm["delete_entity"]("The cloaked stranger") == "Deleted The cloaked stranger."
    assert w.resolve("The cloaked stranger") is None and w.lookup("black cloak") == []
    assert w.events()[0]["entities"] == []  # the entry stays, without it
    assert dm["delete_journal_entry"](event) == f"Deleted journal entry {event}."
    cid = store.create_character({"name": "Bram"}, campaign_id=camp.id)
    assert "a character in the party" in dm["delete_entity"]("Bram")
    assert dm["rename_entity"]("Bram", "Bram the Bold") == "Renamed Bram to Bram the Bold, sheet and all."
    assert store.character(cid)["name"] == "Bram the Bold" and w.resolve("Bram") is None


def test_search_doesnt_reveal_hidden_facts():
    store, camp, w, _ = setup(role="dm", dm_only=True)
    w.record(w.resolve("Morgantha")["id"], "Morgantha sells pies to the party", play(store, w, 1), 1)
    from dataclasses import replace

    player = World(store, replace(camp, role="player"))
    assert player.lookup("night hag") == []          # only the hidden book fact says "night hag"
    assert player.list(q="night hag") == []
    assert [v["name"] for v in player.lookup("pies")] == ["Morgantha"]
    assert [v["name"] for v in w.lookup("night hag")] == ["Morgantha"]  # the DM still finds it


# -- player characters ------------------------------------------------------------


def test_sheet_links_to_the_pc_the_world_already_knows():
    store, camp, w, _ = setup()
    # A session mentioned the character before anyone made a sheet.
    bram = w.ensure_entity("Bram", "pc")
    w.add_alias(bram["id"], "the paladin", play(store, w, 1))
    w.record(bram["id"], "Bram lost his shield", w.start_run("ingest", session=1), 1)
    cid = store.create_character({"name": "Bram"}, campaign_id=camp.id)
    assert store.character(cid)["entity_id"] == bram["id"]
    # An NPC of the same name is a different thing: a sheet never links to it.
    ireena_npc = w.resolve("Ireena")["id"]
    other = store.create_character({"name": "Ireena"}, campaign_id=camp.id)
    assert store.character(other)["entity_id"] != ireena_npc
    assert w.entity(store.character(other)["entity_id"])["kind"] == "pc"


def test_linked_pc_survives_revert_and_follows_merges():
    store, camp, w, _ = setup()
    cid = store.create_character({"name": "Bram"}, campaign_id=camp.id)
    eid = store.character(cid)["entity_id"]
    w.revert_run(play(store, w, 1))  # cleans up orphaned entities: the sheet's isn't one
    assert w.entity(eid) is not None
    # The ingest made a second entity for the same character; merging the sheet's into it moves the link.
    dup = w.ensure_entity("Bram of Vallaki", "pc")["id"]
    w.record(dup, "Bram owes the Martikovs", play(store, w, 2), 2)
    w.merge(dup, eid)
    assert store.character(cid)["entity_id"] == dup


def test_tools_find_a_character_by_any_name_its_entity_has():
    from campaign.tools import ToolContext, build_tools

    store, camp, w, _ = setup()
    cid = store.create_character({"name": "Bram"}, campaign_id=camp.id)
    w.add_alias(store.character(cid)["entity_id"], "the paladin", play(store, w, 1))
    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    assert '"name": "Bram"' in tools["get_character"]("the paladin")
    tools["update_character"]("the paladin", {"hp": {"current": 3, "base": 10}}, "took a hit")
    assert store.character(cid)["sheet"]["hp"]["current"] == 3
    assert [c["id"] for c in store.characters(campaign_ids=[camp.id])] == [cid]  # no duplicate created


def _entry(name, kind="npc", aliases=(), facts=()):
    return {"name": name, "kind": kind, "aliases": list(aliases), "summary": f"{name}.", "pages": [1],
            "facts": list(facts), "stat_block": None}


def test_a_shared_title_doesnt_snowball_entities_together():
    store = Store(":memory:")
    camp = store.create_campaign("Titans", "dm")
    src = store.add_source(camp, "Book", "module", [{"heading": "", "text": "x", "page": 1}])
    w = World(store, camp)
    w.apply_book(src, [
        _entry("Gaius", aliases=["the Titan", "the king"]),
        _entry("Sydon", aliases=["the Titan", "the king"]),
        _entry("Heleka", aliases=["the Titan"]),
        _entry("boar", "monster", aliases=["Boar"]),
        _entry("Dread Boar", "monster", aliases=["the boar"]),
        _entry("Lutheria", "location", aliases=["the Titan's city"]),
        _entry("Lutherian guard", aliases=["Lutheria"]),
    ], key="v1")
    names = {r["name"] for r in w.list()}
    assert names == {"Gaius", "Sydon", "Heleka", "boar", "Dread Boar", "Lutheria", "Lutherian guard"}
    from campaign.world import _page_list
    assert _page_list(list(range(1, 11))) == "1, 2, 3, 4, 5, 6 (+4 more)"
    assert w.resolve("Lutheria")["kind"] == "location"  # its own name wins over someone's alias
    assert w.resolve("Sydon")["name"] == "Sydon"


def test_a_new_apply_version_reapplies_a_cached_catalog(monkeypatch):
    import campaign.world as world_mod

    store, camp, w, src = setup()
    assert w.apply_book(src, BOOK, key="v1") is None
    monkeypatch.setattr(world_mod, "APPLY_VERSION", world_mod.APPLY_VERSION + 1)
    assert w.apply_book(src, BOOK, key="v1")
    assert len(w.list()) == 2


def test_lookup_and_find_entities_stay_a_readable_size():
    import json

    from campaign.tools import PAGE, ToolContext, build_tools

    store = Store(":memory:")
    camp = store.create_campaign("Big", "dm")
    src = store.add_source(camp, "Book", "module", [{"heading": "", "text": "x", "page": 1}])
    w = World(store, camp)
    many = [_entry("Hub", "location", aliases=[f"hub alias {i}" for i in range(30)],
                   facts=[f"fact number {i}" for i in range(80)])]
    many += [_entry(f"Goblin {i}", "monster") for i in range(100)]
    w.apply_book(src, many, key="v1")
    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    hub = json.loads(tools["lookup"]("Hub"))[0]
    assert len(hub["aliases"]) == 12 and hub["aliases_not_shown"] == 18
    assert len(hub["facts"]) == 40 and hub["facts_not_shown"] > 0
    overview = json.loads(tools["find_entities"]())
    assert overview["counts_by_kind"] == {"location": 1, "monster": 100}
    page = json.loads(tools["find_entities"](kind="monster"))
    assert page["total"] == 100 and len(page["entities"]) == PAGE and page["next_offset"] == PAGE
    rest = json.loads(tools["find_entities"](kind="monster", offset=PAGE))
    assert len(rest["entities"]) == 100 - PAGE and "next_offset" not in rest


def test_stat_block_tool_prints_the_book_block():
    from campaign.tools import ToolContext, build_tools

    store, camp, w, src = setup()
    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    text = tools["stat_block"]("the windmill hag")
    assert text.startswith("Morgantha\nAC 17 · HP 112 · Speed 30 ft. · CR 5")
    assert "STR 18 (+4)" in text and "DEX 15 (+2)" in text
    assert "no stat block" in tools["stat_block"]("Ireena")


def test_table_rolls_land_on_the_printed_rows():
    from campaign.tables import covers, roll, row_for

    t = {"dice": "d100", "rows": [{"roll": "01-50", "result": "a"}, {"roll": "51-99", "result": "b"},
                                  {"roll": "00", "result": "c"}]}
    assert row_for(t, 1)["result"] == "a" and row_for(t, 99)["result"] == "b" and row_for(t, 100)["result"] == "c"
    assert covers("18+", 20, 20) and not covers("18+", 17, 20)
    assert covers("1, 2", 2, 6) and covers("3–4", 4, 6) and not covers("5", 6, 6)
    assert 2 <= roll("2d6") <= 12 and 1 <= roll("d%") <= 100


def test_the_same_name_with_another_epithet_is_one_entity():
    store = Store(":memory:")
    camp = store.create_campaign("Titans", "dm")
    src = store.add_source(camp, "Book", "module", [{"heading": "", "text": "x", "page": 1}])
    w = World(store, camp)
    w.apply_book(src, [
        _entry("Sydon, Lord of Storms"), _entry("Sydon, The Lord of Storms"),
        _entry("Versi the Oracle"), _entry("Versi, the Oracle"),
        _entry("Mytros, the Goddess of Dawn"), _entry("Mytros", "location"),
        _entry("Estor Arkelander"), _entry("Telamok Arkelander"),
    ], key="v1")
    assert sorted(r["name"] for r in w.list()) == [
        "Estor Arkelander", "Mytros", "Mytros, the Goddess of Dawn", "Sydon, Lord of Storms",
        "Telamok Arkelander", "Versi the Oracle",
    ]


def test_copies_an_older_apply_made_are_folded_into_one(monkeypatch):
    import campaign.world as world_mod

    store = Store(":memory:")
    camp = store.create_campaign("Titans", "dm")
    src = store.add_source(camp, "Book", "module", [{"heading": "", "text": "x", "page": 1}])
    w = World(store, camp)
    entries = [_entry("Sydon, Lord of Storms"), _entry("Sydon, The Lord of Storms")]
    monkeypatch.setattr(world_mod, "_name_keys", lambda name: {name})  # the rules before epithets matched
    w.apply_book(src, entries, key="v1")
    assert len(w.list()) == 2
    monkeypatch.undo()
    monkeypatch.setattr(world_mod, "APPLY_VERSION", world_mod.APPLY_VERSION + 1)
    w.apply_book(src, entries, key="v1")
    assert [r["name"] for r in w.list()] == ["Sydon, Lord of Storms"]


def test_a_mistaken_write_from_chat_can_be_taken_back():
    from campaign.tools import ToolContext, build_tools

    store, camp, w, _ = setup()
    store.upsert_session(camp, 2)
    ireena = w.resolve("Ireena")["id"]
    w.record(ireena, "Ireena is in Vallaki", play(store, w, 1), 1, attribute="location", value="Vallaki")
    tools = {t.__name__: t for t in build_tools(ToolContext(store, camp))}
    tools["record_fact"]("Ireena", "Ireena is at the Abbey", attribute="location", value="Abbey")
    tools["add_journal_entry"]("The party fought Morgantha.", ["Morgantha"])
    tools["record_fact"]("Bildrath", "Bildrath runs the mercantile", kind="npc")
    abbey = next(f for f in w.facts(ireena) if f["statement"] == "Ireena is at the Abbey")
    entry = w.events()[0]
    bildrath = w.resolve("Bildrath")["id"]
    lives = next(f for f in w.facts(ireena) if f["statement"].startswith("Lives"))

    assert tools["retract_fact"](abbey["id"]) == f"Deleted fact {abbey['id']}."
    assert w.view(ireena)["current"] == {"location": "Vallaki"}  # the value it replaced is current again
    assert tools["delete_journal_entry"](entry["id"]) == f"Deleted journal entry {entry['id']}."
    assert w.events() == [] and w.resolve("Morgantha")  # the book's Morgantha stays
    fact = w.facts(bildrath)[0]
    tools["retract_fact"](fact["id"])
    assert w.resolve("Bildrath") is None  # an entity only that write made goes with it
    # What the book says is changed by digesting it again, not here.
    assert "can't be deleted" in tools["retract_fact"](lives["id"])
    assert "No journal entry" in tools["delete_journal_entry"](12345)


def test_the_schema_is_made_whole_and_the_deployed_database_brought_up_to_it(tmp_path):
    import sqlite3

    from campaign.db import SCHEMA_VERSION, Store

    db = tmp_path / "campaign.db"
    s = Store(db)
    assert s.schema_version == SCHEMA_VERSION
    camp = s.create_campaign("Odyssey", "dm")
    src = s.add_source(camp, "Odyssey", "module", [{"heading": "", "text": "x", "page": 1}])
    s.put_digest("k-root", "branch", {"summary": "The book.", "entities": [], "tables": []}, "m", 10, 5)
    s.put_digest("k-leaf", "leaf", {"summary": "A part.", "entities": [], "tables": []}, "m", 10, 5)
    root, leaf = s.save_digest_plan(src, [
        {"parent": None, "depth": 0, "title": "Odyssey", "page_start": 1, "page_end": 2, "kind": "branch",
         "chars": 0, "key": "k-root"},
        {"parent": 0, "depth": 1, "title": "Part", "page_start": 1, "page_end": 1, "kind": "leaf",
         "chars": 9, "key": "k-leaf"},
    ])
    conv = s.create_conversation(camp, None, "Prep")
    # The deployment as it stands: version 21, when a session had one source.
    s.conn.executescript("""
        ALTER TABLE runs DROP COLUMN recap_before;
        ALTER TABLE aliases DROP COLUMN party_knows_from;
        ALTER TABLE sessions ADD COLUMN source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL;
    """)
    t1 = s.add_source(camp, "Session 1 transcript", "transcript", [{"heading": "", "text": "Night one.", "page": 1}])
    t2 = s.add_source(camp, "Session 2 transcript", "transcript", [{"heading": "", "text": "Quiet.", "page": 1}])
    waiting = s.add_source(camp, "Session 3 transcript", "transcript", [{"heading": "", "text": "Three.", "page": 1}])
    s.conn.executescript(f"""
        INSERT INTO sessions (campaign_id, number, title, summary, source_id)
        VALUES ({camp.id}, 1, 'Night', 'They met Ismark.', {t1}), ({camp.id}, 2, 'Quiet', '', {t2});
        INSERT INTO runs (campaign_id, kind, session, created_at) VALUES ({camp.id}, 'ingest', 1, '2026-11-01');
        PRAGMA user_version = 21;
    """)
    pending = s.add_job(camp, "ingest", {"session": 3, "blob": "b", "name": "s3.txt", "played_on": None}, None)
    s.update_job(pending, state="done", result={"session": 3, "review": "pending", "proposals": [], "source_id": waiting})
    s.close()
    s = Store(db)
    assert s.schema_version == SCHEMA_VERSION
    assert s.digest_status(src)["done"] == 2 and s.conversation(conv)["title"] == "Prep"  # what was there stays
    sessions = s.sessions(camp)
    assert [(x["number"], x["title"], [(u["session"], u["source_id"]) for u in x["uploads"]]) for x in sessions] == [
        (1, "Night", [(1, t1)]), (2, "Quiet", [(2, t2)])]  # session 2's transcript recorded nothing, and is still listed
    review = s.job(pending)["result"]
    assert "source_id" not in review and review["name"] == "s3.txt" and review["blob"]
    assert waiting not in [x["id"] for x in s.sources(camp)]  # it becomes an upload when applied
    assert list((tmp_path / "backups").glob(f"campaign-*-pre-v{SCHEMA_VERSION}.db"))
    s.close()
    assert Store(db).schema_version == SCHEMA_VERSION  # reopening changes nothing

    # Version 22, when a player saw every alias but a DM-only book's: from then, each is known or not.
    store, camp, w, src = setup(role="dm", dm_only=True)
    path = tmp_path / "v22.db"
    store.conn.execute(f"VACUUM INTO '{path}'")
    s = Store(path)
    hag = s.conn.execute("SELECT entity_id FROM aliases WHERE alias = 'the windmill hag'").fetchone()[0]
    run = s.conn.execute("INSERT INTO runs (campaign_id, kind, session, created_at) VALUES (?, 'chat', 3, 'x')",
                         (camp.id,)).lastrowid
    s.conn.executescript(f"""
        INSERT INTO aliases (entity_id, alias, run_id) VALUES ({hag}, 'the baker', {run});
        ALTER TABLE aliases DROP COLUMN party_knows_from;
        PRAGMA user_version = 22;
    """)
    s.close()
    known = dict(Store(path).conn.execute("SELECT alias, party_knows_from FROM aliases WHERE entity_id = ?", (hag,)).fetchall())
    assert known == {"the windmill hag": None, "the baker": 3}

    # A database from a version with no way forward is refused, not guessed at.
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("CREATE TABLE campaigns (id INTEGER PRIMARY KEY); PRAGMA user_version = 9;")
    c.close()
    with pytest.raises(RuntimeError, match="schema version 9"):
        Store(old)
