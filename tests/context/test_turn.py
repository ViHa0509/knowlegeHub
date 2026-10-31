"""GET /turn/context: block text and sections (§3.4)."""
from __future__ import annotations

from datetime import date, timedelta

from app import projects, sqldb, turn, usage

HEADER = ("KH TURN CONTEXT - computed by the hook for this message. It already answers /orchestrator/project_match, "
          "/policy/actions, /findings/list, /entities/match and the lesson query; do NOT call those again this turn.")


def _turn(client, text, **kw):
    r = client.get("/turn/context", params={"text": text}, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_no_project_block(client):
    j = _turn(client, "hello there, thanks a lot")
    assert set(j) >= {"project", "actions", "findings", "entities", "lessons", "my_lessons", "distilled", "near",
                      "repeats", "text"}
    assert j["project"] == {"best": None, "ambiguous": False, "weak_only": False, "matches": []}
    lines = j["text"].split("\n")
    assert lines[0] == HEADER
    assert lines[1] == ("PROJECT: none matched literally - map by meaning, or stay project-less for small talk; a log "
                        "or finding of project-less work goes to your personal project")
    assert lines[2] == ("ACTIONS (pick a label or none; if one fits, GET /policy/plan?action=<label> and follow it):")
    assert "  - 'talk with user': Greetings, thanks, small talk, questions about you or KH, or a message too unclear " \
           "to map to any task." in lines
    assert lines[-1] == "KH LESSONS: none above the floor for the raw message."
    assert not any(line.startswith("FINDINGS") for line in lines)
    assert len(j["actions"]) >= 14 and set(j["actions"][0]) == {"action", "project", "when"}


def test_when_line_short(client):
    a = next(x for x in _turn(client, "hi")["actions"] if x["action"] == "store worklog")
    assert len(a["when"]) == 160 and a["when"].endswith(" the helper...")
    assert a["when"].startswith("Only when the person asks to log work (log this, ghi log, luu worklog)")
    assert turn.short("First sentence. Second one.", 160) == "First sentence."
    assert turn.short("word " * 50, 20) == "word word word wo..."
    assert turn.short("line one\nline two", 160) == "line one"


def test_project_match_findings_entities(client, uid):
    pid = f"tp{uid}"
    projects.get_or_create_project(pid, aliases=[f"turn alpha {uid}"])
    projects.sync_orchestrator_registry()
    client.post("/entities", json={"project": pid, "name": f"Kestrel{uid}", "type": "tool", "role": "scheduler"})
    j = _turn(client, f"please fix the build of Turn Alpha {uid} and Kestrel{uid}")
    assert j["project"]["best"] == pid and j["project"]["matches"][0] == {"project": pid, "term": f"turn alpha {uid}",
                                                                          "on": "alias"}
    t = j["text"]
    assert f"PROJECT: {pid} (matched 'turn alpha {uid}' on alias)" in t
    assert (f"ACTIONS for {pid} (pick a label or none; if one fits, GET /policy/plan?action=<label>&project={pid} "
            "and follow it):") in t
    assert f"FINDINGS ({pid}): none" in t
    assert f"PEOPLE AND THINGS NAMED:\n  - Kestrel{uid} (tool, {pid}) - role: scheduler" in t


def test_ambiguous(client, uid):
    projects.get_or_create_project(f"amb1{uid}", aliases=[f"zed{uid}"])
    projects.get_or_create_project(f"amb2{uid}", aliases=[f"zed{uid}"])
    projects.sync_orchestrator_registry()
    j = _turn(client, f"status of zed{uid}")
    assert j["project"]["best"] is None and j["project"]["ambiguous"] is True
    assert f"PROJECT: AMBIGUOUS between amb1{uid}, amb2{uid} - ask which one" in j["text"]


def test_lessons_block(client, add_row, uid):
    text = f"how to reset the {uid} vpn token"
    lid = add_row(text, project="lesson-learned")
    j = _turn(client, text)
    assert j["lessons"][0]["id"] == lid and j["lessons"][0]["score"] >= 0.99
    assert j["lessons"][0]["title"] == text
    assert ("KH LESSONS near this message (read a whole row with POST /vector/scroll {\"ids\":[...],\"scope\":"
            "\"knowledge\",\"project\":\"lesson-learned\"}):") in j["text"]
    assert j["text"].endswith(f"  - {lid} ({j['lessons'][0]['score']}): {text}")


def test_repeats_block(client):
    today = date.today()
    with sqldb.connect() as c:
        usage.ensure()
        for d in (2, 3, 4):
            sqldb.upsert(c, "usage_daily", ["person", "day_utc", "action_key"], {
                "person": "local", "day_utc": (today - timedelta(days=d)).isoformat(), "action_key": "sign_in/app",
                "hits": 2, "sessions": "[]", "projects": "[]", "sample": "signed in to the app"})
    j = _turn(client, "sign in to the web app again")
    last = (today - timedelta(days=2)).isoformat()
    assert j["repeats"] == [f"sign_in/app on 3 days of the last 14 (6 times, last {last})"]
    assert ("REPEATED ACROSS DAYS (counted by learn-back from your work logs; if this message is one of them, follow "
            f"'suggest a tool' once):\n  - sign_in/app on 3 days of the last 14 (6 times, last {last})") in j["text"]


def test_mask():
    t, n = turn.mask("my password: hunter2xyz and token=abcd1234 Bearer eyJabc.def")
    assert "hunter2xyz" not in t and "abcd1234" not in t and n >= 2
    assert turn.mask("password: wrong")[0] == "password: wrong"
    assert turn.mask("commit 3f2a9c1d")[1] == 0


def test_signed_in_own_lessons_and_session(monkeypatch, client, add_row, uid):
    try:
        from tests.auth_helpers import bearer, enable_signin, make_token
    except Exception:  # pragma: no cover
        import pytest

        pytest.skip("auth helpers not available")
    email = f"t{uid}@x.com"
    own = "personal-" + email.replace("@", "-").replace(".", "-")
    text = f"how do I export the {uid} invoices"
    lid = add_row(text, scope="lesson", project=own)
    pid = f"sess{uid}"
    projects.get_or_create_project(pid)
    projects.sync_orchestrator_registry()
    enable_signin(monkeypatch)
    h = bearer(make_token(email, projects={pid: "editable"}), actor=f"actor-{uid}")
    j = _turn(client, text, headers=h)
    assert j["my_lessons"][0]["id"] == lid and j["my_lessons"][0]["project"] == own
    assert ("YOUR OWN LESSONS near this message (they win over KH LESSONS on overlap; whole row: POST /vector/scroll "
            f"{{\"ids\":[...],\"scope\":\"lesson\",\"project\":\"{own}\"}}):") in j["text"]
    assert j["project"]["session"] is None
    assert "THIS SESSION HAS NO PROJECT YET" in j["text"]
    j2 = _turn(client, f"work on {pid} now", headers=h)
    assert j2["project"]["best"] == pid and j2["project"]["session"]["source"] == "detected"
    assert f"PROJECT: {pid} - BOUND TO THIS SESSION (detected " in j2["text"]
