"""KH 7.x message routes over the v6.5 message store: POST /messages/send (KH picks the channel) and
GET /messages/sent (what the caller sent, delivered per recipient).

POST /messages/send {content, to?, to_actor?, project?, mentions?, private?, everyone?, reply_to?, kind?}:
- everyone=true: an announcement to all KH users (maintainers only, as POST /messages/announce);
- reply_to=<id>: back to the sender of that message (and to the session that sent it);
- project=<id>: a channel post in that project, the mentioned get a notice;
- to=<e-mail>, private=true: the receiver's mailbox;
- to=<e-mail> otherwise: the session's bound space, mentioning them, else private.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app import messages as MS
from app import sqldb


class SendBody(BaseModel):
    content: Any = None
    to: Optional[str] = None
    to_actor: Optional[str] = None
    project: Optional[str] = None
    mentions: Optional[list[str]] = None
    private: bool = False
    everyone: bool = False
    reply_to: Optional[str] = None
    kind: Optional[str] = None


def _bound() -> Optional[str]:
    try:
        from app import personalization

        return personalization._bound_project()
    except Exception:  # noqa: BLE001
        return None


def _original(mid: str) -> Optional[dict]:
    with MS._conn() as c:
        r = c.execute(f"SELECT {', '.join(MS.COLS)} FROM message_stack WHERE id=?", (mid,)).fetchone()
    return MS._msg(r) if r else None


def send(b: SendBody) -> dict:
    text = b.content if isinstance(b.content, str) else ("" if b.content is None else str(b.content))
    if not text.strip():
        raise HTTPException(status_code=400, detail="content is empty")
    if b.everyone:
        out = MS.announce(text, b.kind)
        out.setdefault("channel", "realm")
        return out
    if b.reply_to:
        orig = _original(b.reply_to)
        if orig is None:
            raise HTTPException(status_code=404, detail=f"no message '{b.reply_to}' to answer")
        me, _actor, _m = MS._me()
        if orig["channel"] == "project" and not b.private:
            return MS.post_project(orig["project"], text, [orig["sender"]] if orig["sender"] != me else [], None,
                                   b.kind, None, b.reply_to)
        out = MS.post_direct(orig["sender"], orig.get("sender_actor"), text, b.kind)
        out["reply_to"] = b.reply_to
        return out
    if b.project:
        return MS.post_project(b.project, text, b.mentions or ([b.to] if b.to else []), None, b.kind, None, None)
    if not (b.to or "").strip() and not b.to_actor:
        raise HTTPException(status_code=400, detail="to must be an e-mail address")
    if b.private or b.to_actor:
        return MS.post_direct(b.to, b.to_actor, text, b.kind)
    to = MS._email(b.to or "")
    space = _bound()
    if space:
        try:
            return MS.post_project(space, text, [to], None, b.kind, None, None)
        except HTTPException as e:
            if e.status_code not in (400, 403, 404):
                raise
    return MS.post_direct(to, None, text, b.kind)


def _recipients(c, m: dict) -> list[dict]:
    rec = c.execute("SELECT reader, read_at FROM message_receipt WHERE message_id=?", (m["id"],)).fetchall()
    got: dict[str, str] = {}
    for r in rec:
        email = str(r["reader"]).split("#", 1)[0]
        if email not in got or (r["read_at"] or "") < got[email]:
            got[email] = r["read_at"] or ""
    if m["channel"] == "direct":
        wanted = [m["receiver"]]
    elif m["channel"] == "project":
        wanted = list(m.get("mentions") or [])
    else:
        wanted = sorted(got)
    return [{"to": w, "delivered": w in got, "delivered_at": got.get(w) or None, "opened": w in got,
             "opened_at": got.get(w) or None} for w in wanted]


def sent(mid: Optional[str], limit: int) -> dict:
    me, _actor, _m = MS._me()
    out = []
    with MS._conn() as c:
        if mid:
            rows = c.execute(f"SELECT {', '.join(MS.COLS)} FROM message_stack WHERE id=? AND sender=?",
                             (mid, me)).fetchall()
        else:
            rows = c.execute(f"SELECT {', '.join(MS.COLS)} FROM message_stack WHERE sender=? ORDER BY created_at DESC",
                             (me,)).fetchall()
        for r in rows[:limit]:
            m = MS._msg(r)
            out.append({"id": m["id"], "channel": m["channel"], "project": m["project"], "to": m["receiver"],
                        "to_actor": m.get("to_actor"), "mentions": m.get("mentions") or [], "kind": m["kind"],
                        "created_at": m["created_at"], "expires_at": m.get("expires_at"),
                        "content": _short(m["content"]), "recipients": _recipients(c, m)})
    if mid and not out:
        raise HTTPException(status_code=404, detail=f"no message '{mid}' sent by you")
    return {"count": len(out), "messages": out}


def _short(text: Optional[str], n: int = 200) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else t[:n - 3] + "..."


def attach(app: FastAPI) -> None:
    with sqldb.connect() as c:
        MS.ensure_tables(c)

    @app.post("/messages/send", tags=["messages"])
    def messages_send(body: SendBody):
        return send(body)

    @app.get("/messages/sent", tags=["messages"])
    def messages_sent(id: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):  # noqa: A002
        return sent(id, limit)
