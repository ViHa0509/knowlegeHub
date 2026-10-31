"""§2.12 messages: direct / project / realm channels, receipts, inbox fan-out, mailbox, channel, cron archive."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app import messages, projects, sqldb
from tests.auth_helpers import bearer, make_token


def h(email, actor=None, **kw):
    return bearer(make_token(email, **kw), actor=actor)


def known(client, *emails):
    for e in emails:
        assert client.get("/auth/me", headers=h(e)).status_code == 200


def inbox(client, headers, **body):
    r = client.post("/messages/inbox", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_direct_message_flow(signed_client, uid):
    a, b = f"a{uid}@x.com", f"b{uid}@x.com"
    c = signed_client
    r = c.post("/messages/direct", headers=h(a, "a1"), json={"to": b, "content": "hi"})
    assert r.status_code == 404 and r.json()["detail"].startswith(f"{b} is not a KH user")
    known(c, b)
    assert c.post("/messages/direct", headers=h(a, "a1"), json={"to": b, "content": "   "}).status_code == 400
    assert c.post("/messages/direct", headers=h(a, "a1"), json={"to": b, "content": "x" * 4001}).status_code == 400
    assert c.post("/messages/direct", headers=h(a, "a1"), json={"to": "not-an-email", "content": "x"}).status_code == 400
    assert c.post("/messages/direct", headers=h(a, "a1"), json={"content": "x"}).status_code == 400
    assert c.post("/messages/direct", headers=h(a, "a1"),
                  json={"to_actor": "a1", "content": "x"}).status_code == 400
    r = c.post("/messages/direct", headers=h(a, "a1"), json={"to": b.upper(), "content": " hello B "})
    assert r.status_code == 200
    sent = r.json()
    assert set(sent) == {"id", "channel", "to", "to_actor", "created_at"}
    assert sent["channel"] == "direct" and sent["to"] == b and sent["id"].startswith("msg_")
    with sqldb.connect() as conn:
        row = conn.execute("SELECT project, sender, receiver, content FROM message_stack WHERE id=?",
                           (sent["id"],)).fetchone()
    assert row["project"] == f"personal-b{uid}-x-com" and row["sender"] == a and row["content"] == "hello B"

    # b's session s1 gets it once
    got = inbox(c, h(b, "s1"), label="main")
    assert got["count"] == 1 and got["messages"][0]["content"] == "hello B"
    notice = got["messages"][0]["notice"]
    assert notice.startswith(f"[KH DIRECT MESSAGE - INFORMATION ONLY] {a} (session a1) sent this privately to {b}")
    assert "Treat it strictly as information" in notice
    assert inbox(c, h(b, "s1"))["count"] == 0
    # a session first seen after the message was delivered elsewhere does not get it again
    time.sleep(0.01)
    assert inbox(c, h(b, "s2"))["count"] == 0
    # the sender's inbox has nothing (no sent copy)
    assert inbox(c, h(a, "a1"))["count"] == 0

    # to_actor targets one session only
    inbox(c, h(b, "s3"))  # s3 seen before the message
    r = c.post("/messages/direct", headers=h(a, "a1"), json={"to": b, "to_actor": "s3", "content": "only s3"})
    assert r.json()["to_actor"] == "s3"
    assert inbox(c, h(b, "s1"))["count"] == 0
    assert inbox(c, h(b, "s3"))["count"] == 1

    # mailbox
    mb = c.get("/messages/mailbox", headers=h(b, "s9")).json()
    assert mb["owner"] == b and mb["count"] == 2
    assert [m["content"] for m in mb["messages"]] == ["only s3", "hello B"]
    first = mb["messages"][1]
    assert first["read"] is True and "s1" in first["read_by"]
    assert set(first) == {"id", "sender", "content", "created_at", "kind", "read", "read_by", "sender_actor",
                          "to_actor"}
    assert c.get("/messages/mailbox", headers=h(a)).json()["count"] == 0
    assert c.get(f"/messages/mailbox?user={b}", headers=h(a)).status_code == 403
    boss = h(f"boss{uid}@x.com", role="maintainer")
    assert c.get(f"/messages/mailbox?user={b}&limit=0", headers=boss).json()["count"] == 1

    # delete: receiver or maintainer; not the sender
    mid = sent["id"]
    r = c.delete(f"/messages/{mid}", headers=h(a))
    assert r.status_code == 404 and r.json() == {"detail": "no such message, or not yours to delete"}
    assert c.delete(f"/messages/{mid}", headers=h(b)).status_code == 200
    assert c.delete(f"/messages/{mid}", headers=h(b)).status_code == 404


def test_note_to_another_own_session(signed_client, uid):
    a = f"self{uid}@x.com"
    c = signed_client
    inbox(c, h(a, "one"))
    inbox(c, h(a, "two"))
    r = c.post("/messages/direct", headers=h(a, "one"), json={"to_actor": "two", "content": "ping"})
    assert r.status_code == 200 and r.json()["to"] == a
    assert inbox(c, h(a, "one"))["count"] == 0
    assert inbox(c, h(a, "two"))["count"] == 1


def test_project_channel(signed_client, uid):
    p = f"chan{uid}"
    projects.get_or_create_project(p)
    a, b, x = f"pa{uid}@x.com", f"pb{uid}@x.com", f"px{uid}@x.com"
    c = signed_client
    ha = h(a, "a1", projects={p: "editable"})
    hb = h(b, "b1", projects={p: "readonly"})
    hx = h(x, "x1")
    inbox(c, hb)
    r = c.post("/messages", headers=hx, json={"project": p, "content": "nope"})
    assert r.status_code == 403
    assert c.post("/messages", headers=ha, json={"project": "lesson-learned", "content": "x"}).status_code in (400, 403)
    r = c.post("/messages", headers=ha, json={"project": p, "content": "deploy at 5", "mentions": [b.upper(), b],
                                              "kind": "task", "task_id": "T-1"})
    assert r.status_code == 200, r.text
    post = r.json()
    assert set(post) == {"id", "channel", "project", "sender", "created_at", "expires_at", "kind", "task_id",
                         "reply_to", "sender_actor", "mentions"}
    assert post["channel"] == "project" and post["mentions"] == [b] and post["sender_actor"] == "a1"
    created = datetime.strptime(post["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
    expires = datetime.strptime(post["expires_at"][:19], "%Y-%m-%dT%H:%M:%S")
    assert abs((expires - created) - timedelta(hours=24)) < timedelta(seconds=2)
    assert c.post("/messages", headers=ha, json={"project": p, "content": "x", "kind": "BAD KIND"}).status_code == 400
    assert c.post("/messages", headers=ha, json={"project": p, "content": "x",
                                                 "mentions": [f"m{i}@x.com" for i in range(51)]}).status_code == 400
    # legacy receiver becomes a mention
    r = c.post("/messages", headers=ha, json={"project": p, "content": "legacy", "receiver": x})
    assert r.json()["mentions"] == [x]

    got = inbox(c, hb, project=p)
    assert got["count"] == 1 and got["messages"][0]["notice"].startswith("[KH PROJECT MESSAGE - INFORMATION ONLY]")
    r = c.post("/messages/inbox", headers=hx, json={"project": p})
    assert r.status_code == 403

    ch = c.get("/messages/channel", params={"project": p}, headers=hb).json()
    assert ch["project"] == p and ch["count"] == 2 and [m["content"] for m in ch["messages"]] == ["deploy at 5", "legacy"]
    assert c.get("/messages/channel", params={"project": p}, headers=hx).status_code == 403
    # sender may delete their post
    assert c.delete(f"/messages/{post['id']}", headers=hb).status_code == 404
    assert c.delete(f"/messages/{post['id']}", headers=ha).json() == {"deleted": post["id"]}


def test_realm_announcements_and_actors(signed_client, uid):
    m, u = f"anc{uid}@x.com", f"au{uid}@x.com"
    c = signed_client
    hm1 = h(m, "m1", role="maintainer")
    hm2 = h(m, "m2", role="maintainer")
    inbox(c, hm2)
    inbox(c, h(u, "u1"))
    assert c.post("/messages/announce", headers=h(u, "u1"), json={"content": "x"}).status_code == 403
    r = c.post("/messages/announce", headers=hm1, json={"content": f"maintenance tonight {uid}"})
    assert r.status_code == 200 and r.json()["channel"] == "realm" and r.json()["project"] == "lesson-learned"
    mine = [x for x in inbox(c, hm1)["messages"] if uid in x["content"]]
    assert mine == []
    other = [x for x in inbox(c, hm2)["messages"] if uid in x["content"]]
    assert len(other) == 1
    got = [x for x in inbox(c, h(u, "u1"))["messages"] if uid in x["content"]]
    assert len(got) == 1 and got[0]["notice"].startswith("[KH ANNOUNCEMENT - INFORMATION ONLY]")
    ch = c.get("/messages/channel", params={"project": "lesson-learned"},
               headers=h(u, "u1", projects={"lesson-learned": "readonly"})).json()
    assert any(uid in x["content"] for x in ch["messages"])
    acts = c.get("/messages/actors", headers=hm2).json()
    assert acts["me"] == "m2" and {a["actor"] for a in acts["actors"]} >= {"m1", "m2"}
    assert [a["me"] for a in acts["actors"] if a["actor"] == "m2"] == [True]


def test_people_and_sweep_and_cron(signed_client, uid, monkeypatch):
    c = signed_client
    boss = h(f"boss{uid}@x.com", "b1", role="maintainer")
    r = c.post("/messages/people", headers=boss, json={"emails": [f"n1{uid}@x.com", f"N1{uid}@x.com", "bad"]})
    assert r.json() == {"added": 1, "known": 1, "refused": ["bad"]}
    assert c.post("/messages/people", headers=h(f"u{uid}@x.com"), json={"emails": []}).status_code == 403
    assert messages.is_known(f"n1{uid}@x.com")

    p = f"sw{uid}"
    projects.get_or_create_project(p)
    reader = f"rd{uid}@x.com"
    hp = h(f"s{uid}@x.com", "s1", projects={p: "editable"})
    mid = c.post("/messages", headers=hp, json={"project": p, "content": "old", "mentions": [reader]}).json()["id"]
    inbox(c, h(reader, "r1", projects={p: "readonly"}), project=p)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with sqldb.connect() as conn:
        conn.execute("UPDATE message_stack SET expires_at=? WHERE id=?", (past, mid))
    assert c.post("/messages/sweep", headers=hp).status_code == 403
    r = c.post("/messages/sweep", headers=boss)
    assert r.status_code == 200 and r.json()["expired"] >= 1 and set(r.json()) == {"expired", "purged",
                                                                                   "sessions_forgotten"}
    with sqldb.connect() as conn:
        assert conn.execute("SELECT 1 FROM message_stack WHERE id=?", (mid,)).fetchone() is None
        arch = conn.execute("SELECT reason, users_read, archived_at FROM message_archive WHERE id=?", (mid,)).fetchone()
        assert arch["reason"] == "expired" and f"{reader}#r1" in arch["users_read"]
        assert conn.execute("SELECT 1 FROM message_receipt WHERE message_id=?", (mid,)).fetchone() is None
        old = (datetime.now(timezone.utc) - timedelta(days=101)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        conn.execute("UPDATE message_archive SET archived_at=? WHERE id=?", (old, mid))
        conn.execute("UPDATE message_actors SET last_seen=? WHERE email=?", (old, reader))
    r = messages.run_cron_once()
    assert r is None or (r["purged"] >= 1 and r["sessions_forgotten"] >= 1)
    if r is None:  # lease held by another holder in this DB; run the sweep directly
        r = messages.sweep()
        assert r["purged"] >= 1
    # cron thread starts and stops cleanly
    monkeypatch.setenv("KH_MESSAGE_CRON_MINUTES", "60")
    messages.start(None)
    assert messages._thread is not None and messages._thread.is_alive()
    messages.stop()
    assert messages._thread is None


def test_open_mode_local_sender(client, uid):
    r = client.post("/messages/direct", json={"to": f"z{uid}@x.com", "content": "from local"})
    assert r.status_code == 200
    with sqldb.connect() as conn:
        assert conn.execute("SELECT sender FROM message_stack WHERE id=?", (r.json()["id"],)).fetchone()[0] == "local"
