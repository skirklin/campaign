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
    r = client.post("/api/characters/import", files={"file": ("c.json", EXAMPLE.read_bytes())})
    assert r.status_code == 201
    name = r.json()["sheet"]["name"]
    assert client.get("/api/characters").json()[0]["hp"]["current"] == 27

    r = client.post(f"/api/characters/{name}/hp", json={"delta": -30})
    assert r.json()["sheet"]["hp"]["current"] == 0

    sheet = r.json()["sheet"]
    sheet["resources"][0]["used"] = 3
    sheet["abilities"]["wis"] = 18
    r = client.put(f"/api/characters/{name}", json={"sheet": sheet, "reason": "level up"})
    assert r.json()["derived"]["spell_save_dc"] == 14
    assert r.json()["log"][0]["reason"] == "level up"

    r = client.post(f"/api/characters/{name}/rest", json={"long": True})
    assert r.json()["sheet"]["hp"]["current"] == 27
    assert r.json()["sheet"]["resources"][0]["used"] == 0

    sheet = r.json()["sheet"] | {"hp": {"max": 27, "current": "lots", "temp": 0}}
    assert client.put(f"/api/characters/{name}", json={"sheet": sheet}).status_code == 422

    sheet["hp"]["current"] = 5
    sheet["name"] = "Brannoc the Bold"
    assert client.put(f"/api/characters/{name}", json={"sheet": sheet}).status_code == 200
    assert client.get(f"/api/characters/{name}").status_code == 404
    assert client.get("/api/characters/Brannoc the Bold").json()["sheet"]["hp"]["current"] == 5

    assert client.post("/api/characters", json={"name": "Brannoc the Bold"}).status_code == 409
    assert client.delete("/api/characters/Brannoc the Bold").status_code == 204


def test_reference_upload_and_search(client):
    assert client.post("/api/campaigns", json={"name": "Strahd", "role": "player"}).status_code == 201
    assert client.post("/api/campaigns", json={"name": "Strahd"}).status_code == 409
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
        job = client.get(f"/api/jobs/{job_id}").json()
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
    for _ in range(100):
        src = client.get("/api/campaigns/strahd/sources").json()[0]
        if not src["digest"]["running"]:
            break
        time.sleep(0.05)
    assert src["digest"]["done"] == src["digest"]["total"] == 8
    assert src["digest"]["entities"] == 3 and src["has_original"]

    outline = client.get(f"/api/campaigns/strahd/sources/{r['id']}/outline").json()
    assert outline[0]["parent_id"] is None and outline[0]["summary"].startswith("Branch summary")
    hits = client.get(f"/api/campaigns/strahd/sources/{r['id']}/entities", params={"q": "windmill hag"}).json()
    assert "Morgantha" in [e["name"] for e in hits]
    assert client.get(f"/api/campaigns/strahd/sources/{r['id']}/original").content == book

    # Re-uploading the same file is free, and nothing is left to digest.
    again = client.post("/api/campaigns/strahd/sources", files={"file": ("book.md", book)},
                        data={"title": "book", "digest_now": "true"}).json()
    assert again == {"id": r["id"], "chunks": r["chunks"], "pending": 0, "digesting": False}
    assert client.post(f"/api/campaigns/strahd/sources/{r['id']}/digest").status_code == 202

    info = client.get("/api/info").json()
    assert info["schema_version"] >= 2 and info["originals"]["files"] == 1
