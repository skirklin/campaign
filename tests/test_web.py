import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campaign import agent as agent_mod
from campaign.web import create_app

from test_agent import FakeClient, text, tool_use

EXAMPLE = Path(__file__).parent.parent / "examples" / "character.json"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "web.db"))


def test_index_and_static(client):
    assert "app.js" in client.get("/").text
    assert client.get("/static/sheet.js").status_code == 200


def test_character_lifecycle(client):
    # A character belongs to a campaign.
    assert client.post("/api/characters/import", files={"file": ("c.json", EXAMPLE.read_bytes())}).status_code == 422
    client.post("/api/campaigns", json={"name": "Strahd"})
    client.post("/api/campaigns", json={"name": "Avernus"})
    r = client.post("/api/characters/import", files={"file": ("c.json", EXAMPLE.read_bytes())},
                    data={"campaign": "strahd"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert r.json()["can_edit"] and r.json()["campaign"] == {"slug": "strahd", "name": "Strahd"}
    assert client.get("/api/characters").json()[0]["hp"]["current"] == 27
    assert [c["id"] for c in client.get("/api/campaigns/strahd/characters").json()] == [cid]
    assert client.get("/api/campaigns/avernus/characters").json() == []

    r = client.post(f"/api/characters/{cid}/hp", json={"delta": -30})
    assert r.json()["sheet"]["hp"]["current"] == 0

    sheet = r.json()["sheet"]
    sheet["resources"][0]["used"] = 3
    sheet["abilities"]["wis"] = 18
    r = client.put(f"/api/characters/{cid}", json={"sheet": sheet, "reason": "level up"})
    assert r.json()["derived"]["spell_save_dc"] == 14
    assert r.json()["log"][0]["reason"] == "level up"

    r = client.post(f"/api/characters/{cid}/rest", json={"long": True})
    assert r.json()["sheet"]["hp"]["current"] == 27
    assert r.json()["sheet"]["resources"][0]["used"] == 0

    sheet = r.json()["sheet"] | {"hp": {"max": 27, "current": "lots", "temp": 0}}
    assert client.put(f"/api/characters/{cid}", json={"sheet": sheet}).status_code == 422

    # The sheet is a player character in the campaign's world.
    eid = r.json()["entity_id"]
    entity = client.get(f"/api/campaigns/strahd/entities/{eid}").json()
    assert entity["kind"] == "pc" and entity["name"] == sheet["name"]
    assert entity["character"] == {"id": cid, "name": sheet["name"]}

    old_name = sheet["name"]
    sheet["hp"]["current"] = 5
    sheet["name"] = "Brannoc the Bold"  # renaming keeps the id, and the world follows
    assert client.put(f"/api/characters/{cid}", json={"sheet": sheet}).status_code == 200
    renamed = client.get(f"/api/characters/{cid}").json()
    assert renamed["sheet"]["name"] == "Brannoc the Bold" and renamed["entity_id"] == eid
    entity = client.get(f"/api/campaigns/strahd/entities/{eid}").json()
    assert entity["name"] == "Brannoc the Bold" and old_name in entity["aliases"]

    # Copying takes the character to another campaign as a separate sheet.
    copy = client.post(f"/api/characters/{cid}/copy", json={"campaign": "avernus"})
    assert copy.status_code == 201
    copy = copy.json()
    assert copy["id"] != cid and copy["campaign"]["slug"] == "avernus" and copy["entity_id"] != eid
    assert copy["copied_from"] == {"id": cid, "name": "Brannoc the Bold", "campaign": {"slug": "strahd", "name": "Strahd"}}
    assert client.post(f"/api/characters/{cid}/hp", json={"delta": -2}).json()["sheet"]["hp"]["current"] == 3
    assert client.get(f"/api/characters/{copy['id']}").json()["sheet"]["hp"]["current"] == 5  # independent
    assert client.post(f"/api/characters/{cid}/copy", json={"campaign": "strahd"}).status_code == 422
    assert client.post(f"/api/characters/{cid}/copy", json={"campaign": "avernus"}).status_code == 409

    # Names are unique within a campaign, not across them.
    assert client.post("/api/characters", json={"name": "Bob", "campaign": "strahd"}).status_code == 201
    assert client.post("/api/characters", json={"name": "Bob", "campaign": "strahd"}).status_code == 409
    other = client.post("/api/characters", json={"name": "Bob", "campaign": "avernus"})
    assert other.status_code == 201 and other.json()["campaign"] == {"slug": "avernus", "name": "Avernus"}
    assert client.delete(f"/api/characters/{cid}").status_code == 204
    assert client.get(f"/api/characters/{cid}").status_code == 404
    assert client.get(f"/api/characters/{copy['id']}").json()["copied_from"] is None  # the original is gone


def test_reference_upload_and_search(client):
    assert client.post("/api/campaigns", json={"name": "Strahd", "role": "player"}).status_code == 201
    again = client.post("/api/campaigns", json={"name": "Strahd"})
    assert again.status_code == 201 and again.json()["slug"] == "strahd-2"  # same name, new slug
    md = b"# Vallaki\n## Blue Water Inn\nUrwin Martikov runs the inn.\n"
    r = client.post(
        "/api/campaigns/strahd/sources",
        files={"file": ("notes.md", md)}, data={"kind": "notes", "dm_only": "false"},
    )
    assert r.json()["chunks"] == 1
    client.post("/api/campaigns/strahd/sources", files={"file": ("cos.md", b"# Secret\nUrwin is a wereraven.")}, data={"kind": "module"})
    sources = client.get("/api/campaigns/strahd/sources").json()
    assert [s["dm_only"] for s in sources] == [0, 1]
    # player campaign: the DM-only module is invisible to search
    hits = client.get("/api/campaigns/strahd/search", params={"q": "urwin"}).json()
    assert [h["source"] for h in hits] == ["notes"]
    assert client.post("/api/campaigns/strahd/sources", files={"file": ("x.docx", b"")}).status_code == 422
    assert client.delete(f"/api/campaigns/strahd/sources/{sources[1]['id']}").status_code == 204


def test_ingest_job_runs_in_background(client, monkeypatch):
    fake = FakeClient([
        ([tool_use("t1", "record_fact", entity="Ismark", kind="npc", statement="Ismark is alive",
                   attribute="status", value="alive")], "tool_use"),
        ([tool_use("t2", "add_journal_entry", text="Met Ismark at the tavern.", entities=["Ismark"])], "tool_use"),
        ([tool_use("t3", "finish_session", title="Blood on the Vine", summary="They met Ismark.")], "tool_use"),
        ([text("All recorded.")], "end_turn"),
    ])
    monkeypatch.setattr(agent_mod.anthropic, "Anthropic", lambda: fake)
    client.post("/api/campaigns", json={"name": "Strahd", "role": "dm"})
    assert client.post("/api/campaigns/strahd/ingest", data={"text": ""}).status_code == 422

    r = client.post(
        "/api/campaigns/strahd/ingest",
        files={"file": ("s1.txt", io.BytesIO(b"DM: A man in a cloak..."))},
        data={"played_on": "2026-10-25"},
    )
    job_id = r.json()["job"]
    for _ in range(100):
        job = client.get(f"/api/campaigns/strahd/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert job["result"]["title"] == "Blood on the Vine"
    assert any(e["kind"] == "tool" and "record_fact" in e["text"] for e in job["log"])

    sessions = client.get("/api/campaigns/strahd/sessions").json()
    assert sessions["sessions"][0]["played_on"] == "2026-10-25" and sessions["next"] == 2
    ismark = client.get("/api/campaigns/strahd/entities").json()["entities"][0]
    assert ismark["name"] == "Ismark" and ismark["current"] == {"status": "alive"}
    detail = client.get(f"/api/campaigns/strahd/entities/{ismark['id']}").json()
    assert detail["events"] == [{"session": 1, "text": "Met Ismark at the tavern."}]

    assert client.get("/api/campaigns/strahd/journal").json()[0]["session"] == 1
    assert client.post("/api/campaigns/strahd/sessions/1/revert").json()["entities_removed"] == 1
    assert client.get("/api/campaigns/strahd/entities").json()["entities"] == []


def test_digest_endpoints_and_original_kept(client, monkeypatch):
    from campaign import digest
    from test_digest import BOOK, FakeDigest

    monkeypatch.setattr(digest, "MIN_LEAF_CHARS", 1)
    fake = FakeDigest()
    monkeypatch.setattr(digest.anthropic, "Anthropic", lambda: fake)
    client.post("/api/campaigns", json={"name": "Strahd", "role": "dm"})
    book = BOOK.format(intro="", church="Donavich prays.").encode()
    r = client.post("/api/campaigns/strahd/sources", files={"file": ("book.md", book)},
                    data={"kind": "module", "digest_now": "true"}).json()
    assert r["pending"] == 8 and r["digesting"] is True
    assert r["estimate"]["calls"] == 8 and r["estimate"]["usd"] > 0
    for _ in range(100):
        src = client.get("/api/campaigns/strahd/sources").json()[0]
        if not src["digest"]["running"]:
            break
        time.sleep(0.05)
    assert src["digest"]["done"] == src["digest"]["total"] == 8
    assert src["digest"]["entities"] == 3 and src["has_original"]
    # The fake reports 100 input and 10 output tokens per call, at Opus prices.
    assert src["digest"]["spent_usd"] == pytest.approx(8 * (100 * 4 + 10 * 20) / 1e6)
    assert "estimate" not in src["digest"]  # nothing left to estimate

    outline = client.get(f"/api/campaigns/strahd/sources/{r['id']}/outline").json()
    assert outline[0]["parent_id"] is None and outline[0]["summary"].startswith("Branch summary")
    leaf = next(n for n in outline if n["is_leaf"] and n["entity_count"])
    node = client.get(f"/api/campaigns/strahd/sources/{r['id']}/outline/{leaf['id']}").json()
    assert node["summary"] == leaf["summary"] and len(node["entities"]) == leaf["entity_count"]
    assert client.get(f"/api/campaigns/strahd/sources/{r['id'] + 1}/outline/{leaf['id']}").status_code == 404
    hits = client.get(f"/api/campaigns/strahd/sources/{r['id']}/entities", params={"q": "windmill hag"}).json()
    assert "Morgantha" in [e["name"] for e in hits]
    assert client.get(f"/api/campaigns/strahd/sources/{r['id']}/original").content == book

    # Re-uploading the same file is free, and nothing is left to digest.
    again = client.post("/api/campaigns/strahd/sources", files={"file": ("book.md", book)},
                        data={"title": "book", "digest_now": "true"}).json()
    assert again.pop("estimate")["calls"] == 0
    assert again == {"id": r["id"], "chunks": r["chunks"], "pending": 0, "digesting": False}
    assert client.post(f"/api/campaigns/strahd/sources/{r['id']}/digest").status_code == 202

    info = client.get("/api/info").json()
    assert info["schema_version"] >= 2 and info["originals"]["files"] == 1


def test_character_from_a_scan(client, monkeypatch):
    from campaign import sheetscan
    from campaign.charsheet import ClassLevel, HitPoints

    scan = sheetscan.ScannedSheet(
        name="Ireth", player="Sam", species="Elf", background="Sage", alignment="NG",
        classes=[ClassLevel(name="Wizard", level=3)], xp=900,
        abilities=sheetscan.Abilities(strength=8, dexterity=14, constitution=12, intelligence=17, wisdom=12, charisma=10),
        save_proficiencies=["Intelligence", "wis"], skill_proficiencies=["Arcana", "History", "Basket weaving"],
        skill_expertise=[], ac=12, speed=30, hp=HitPoints(max=17, current=17), attacks=[], resources=[],
        spellcasting_ability="Intelligence", spells=[], inventory=[], currency=sheetscan.Coins(gp=12),
        features=["Arcane Recovery"], proficiencies=["Daggers"], personality="", notes="", unreadable=["Wisdom score"],
    )
    seen = {}

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return type("M", (), {"stop_reason": "end_turn", "parsed_output": scan})()

    class Client:
        class beta:
            class messages:
                @staticmethod
                def stream(**kw):
                    seen.update(kw)
                    return Stream()

    monkeypatch.setattr(sheetscan.anthropic, "Anthropic", lambda: Client())
    client.post("/api/campaigns", json={"name": "Strahd"})
    r = client.post("/api/campaigns/strahd/characters/from-scan", files={"file": ("ireth.pdf", b"%PDF-1.7 fake")})
    assert r.status_code == 201
    body = r.json()
    sheet = body["sheet"]
    assert sheet["name"] == "Ireth" and sheet["abilities"]["int"] == 17 and sheet["save_proficiencies"] == ["int", "wis"]
    assert sheet["skill_proficiencies"] == ["arcana", "history"] and sheet["currency"]["gp"] == 12
    assert body["derived"]["spell_save_dc"] == 8 + 3 + 2
    assert body["unreadable"] == ["Wisdom score", "skill: Basket weaving"]
    assert body["log"][0]["reason"] == "read from ireth.pdf" and body["entity_id"]
    assert seen["messages"][0]["content"][0]["type"] == "document"
    assert client.post("/api/campaigns/strahd/characters/from-scan",
                       files={"file": ("notes.txt", b"hi")}).status_code == 422
    assert client.post("/api/campaigns/strahd/characters/from-scan",
                       files={"file": ("ireth.png", b"png")}).status_code == 409  # same name again
