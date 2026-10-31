"""Usage repeats (§3.12) and the personalization render / legacy guards (§3.9)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import personalization, usage


def test_key_of():
    assert usage.key_of("signed in to the web app with SSO") == "sign_in/app"
    assert usage.key_of("deploying the server again") == "deploy/server"
    assert usage.key_of("hello how are you") is None


def test_learn_and_repeats(client, add_row, uid):
    p = f"us{uid}"
    for i in range(3):
        add_row(f"backup of the database {uid} run {i}", scope="log", project=p, author=f"u{uid}@x.com",
                actor=f"s{i}")
    today = datetime.now(timezone.utc).date().isoformat()  # log ts are UTC
    rep = usage.learn(today, [p])
    assert rep["keyed"] >= 3
    person = "personal-u" + uid + "-x-com"
    rows = usage.rows_of(person)
    r = next(x for x in rows if x["key"] == "backup/database")
    assert r["hits"] == 3 and r["projects"] == [p] and len(r["sample"]) <= 120
    assert usage.repeats(person, min_days=1)[0]["key"] == "backup/database"
    assert usage.repeats(person) == []  # one day only, min_days 3
    assert usage.learn(today, [p])["rows"] == rep["rows"]  # idempotent
    j = client.get("/usage/repeats", params={"min_days": 1}).json()
    assert j["person"] == "local"
    assert client.get("/usage/repeats", params={"days": 91}).status_code == 422
    assert client.post("/usage/learn", json={"day": "bad"}).status_code == 422
    assert client.post("/usage/learn", json={"days_back": 0, "projects": [p]}).status_code == 200


def test_personalization_render_open_mode(client, add_row, uid):
    r = client.get("/personalization/render")
    assert r.status_code == 200 and r.text.startswith(personalization._HEADER)
    add_row(f"Reply short, keyword-driven {uid}.", scope="personalization")
    t = client.get("/personalization/render").text
    assert f"\n- Reply short, keyword-driven {uid}." in t


def test_legacy_rows_in_kernel_and_guards(client, add_row, uid):
    pid = f"personal-leg-{uid}"
    add_row(f"Call me Boss {uid}.", scope="personalization", project=pid, ts="2026-01-02T00:00:00Z")
    add_row(f"Old retired pref {uid}.", scope="personalization", project=pid, status="retired")
    add_row(f"Confirm every git push {uid}.", scope="personalization", project=pid, ts="2026-01-03T00:00:00Z",
            guard_tool="Bash", guard_decision="deny", guard_when='{"cmd": ["push"]}')
    rows = personalization.own_rows(pid)
    assert rows == [f"Call me Boss {uid}.", f"Confirm every git push {uid}."]
    text = client.get("/kernel/render", params={"personal": pid}).text
    assert (f"PERSONALIZATION NOT YET TAGGED (wins over the common rules on overlap, except the locked ones):\n"
            f"- Call me Boss {uid}.\n- Confirm every git push {uid}.") in text
    assert "NOT YET TAGGED" not in client.get("/kernel/render", params={"personal": pid, "legacy": False}).text
    g = client.get("/personalization/guards", params={"tool": "Bash", "personal": pid}).json()["guards"]
    assert g[-1]["decision"] == "deny" and g[-1]["when"] == {"cmd": ["push"]}
