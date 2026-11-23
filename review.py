"""Draft proposals and atomic reviewer decisions for the local wiki."""
import json
import uuid

from wiki import SLUG, add_page, allowed, connect, page, publish, verify_source


def propose(path, *, doc, sec, revision, body, source, actor, role):
    if role != "editor":
        raise ValueError("Only editors may propose changes")
    if not body.strip() or len(body) > 12000:
        raise ValueError("Invalid body")
    with connect(path, True) as db:
        if not allowed(db, doc, actor, edit=True):
            raise ValueError("Unknown page")
        baseline = page(db, doc, section=sec, principal=actor)
        if baseline["revision"] != revision:
            raise ValueError(f"Version conflict: current revision {baseline['revision']}")
        verify_source(db, source, actor)
        return insert_draft(db, doc, sec, revision, body, source, actor)


def propose_page(path, *, doc, title, lang, sections, source, actor, role):
    if role != "editor":
        raise ValueError("Only editors may propose articles")
    if not SLUG.fullmatch(doc) or not 1 <= len(title.strip()) <= 255 or lang not in {"en", "ru"}:
        raise ValueError("Invalid article identity")
    if not 1 <= len(sections) <= 30 or any(
        not 1 <= len(h.strip()) <= 255 or not 1 <= len(t.strip()) <= 12000 for h, t in sections
    ):
        raise ValueError("Invalid article sections")
    with connect(path, True) as db:
        if db.execute("SELECT 1 FROM pages WHERE id=?", (doc,)).fetchone():
            raise ValueError("Page ID already exists")
        verify_source(db, source, actor)
        body = json.dumps({"title": title, "lang": lang, "sections": sections}, ensure_ascii=False)
        return insert_draft(db, doc, 0, 0, body, source, actor)


def insert_draft(db, doc, sec, revision, body, source, actor):
    key = uuid.uuid4().hex
    db.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL)",
               (key, doc, sec, revision, body, source, actor))
    return {"id": key, "url": f"/review/{key}", "state": "pending"}


def read_draft(db, key, actor, role):
    row = db.execute("SELECT * FROM drafts WHERE id=?", (key,)).fetchone()
    if not row or role != "editor":
        raise ValueError("Unknown draft")
    result = dict(row)
    if result["sec"]:
        if not allowed(db, result["doc"], actor, edit=True):
            raise ValueError("Unknown draft")
        baseline = page(db, result["doc"], result["base_revision"], result["sec"], principal=actor)
        result["title"] = baseline["title"]
        result["before"] = baseline["sections"][0]["body"]
    else:
        article = json.loads(result["body"])
        result["title"], result["before"] = article["title"], ""
        result["body"] = "\n\n".join(f"{h}\n{t}" for h, t in article["sections"])
    try:
        verify_source(db, result["source"], actor)
    except ValueError as exc:
        raise ValueError("Unknown draft") from exc
    result["can_decide"] = result["state"] == "pending" and actor != result["author"]
    return result


def decide(path, key, decision, *, actor, role):
    if decision not in {"approve", "reject"}:
        raise ValueError("Unknown decision")
    with connect(path, True) as db:
        draft = read_draft(db, key, actor, role)
        old_state = draft["state"]
        status = {"approve": "approved", "reject": "rejected"}[decision]
        if old_state == status and draft["reviewer"] == actor:
            return {"id": key, "state": status, "revision": draft["published_revision"]}
        if not draft["can_decide"]:
            raise ValueError("Draft requires a different editor and a pending review")
        doc, sec, base, source = (draft[k] for k in ("doc", "sec", "base_revision", "source"))
        revision = None
        if decision == "approve":
            if sec:
                result = publish(db, doc=doc, sec=sec, base_revision=base, body=draft["body"],
                                 source=source, actor=actor)
                revision = result["revision"]
            else:
                payload = db.execute("SELECT body FROM drafts WHERE id=?", (key,)).fetchone()[0]
                article = json.loads(payload)
                add_page(db, doc, article["title"], article["lang"], article["sections"])
                db.execute("UPDATE edits SET source=? WHERE doc=?", (source, doc))
                revision = 1
        if sec or revision is not None:
            db.execute("INSERT INTO audit(doc,sec,rev,actor,action,source) VALUES(?,?,?,?,?,?)",
                       (doc, sec, revision or base, actor, f"{status} draft by {draft['author']}", source))
        db.execute("UPDATE drafts SET state=?,reviewer=?,published_revision=? WHERE id=?",
                   (status, actor, revision, key))
        return {"id": key, "state": status, "revision": revision}
