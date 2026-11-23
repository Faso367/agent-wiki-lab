"""Versioned local wiki. SQLite transactions also publish section search updates."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from corpus import STATE, documents, verify_inputs
from retrieval import terms

DEFAULT_DB = STATE / "wiki.sqlite"
SLUG = re.compile(r"[a-zA-Z0-9_-]{1,100}\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY,title TEXT NOT NULL,lang TEXT NOT NULL,rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS edits(doc TEXT NOT NULL,sec INTEGER NOT NULL,rev INTEGER NOT NULL,
 heading TEXT NOT NULL,body TEXT NOT NULL,source TEXT,PRIMARY KEY(doc,sec,rev));
CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY,signature TEXT NOT NULL,reply TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actor_requests(actor TEXT NOT NULL, key TEXT NOT NULL,
 signature TEXT NOT NULL, reply TEXT NOT NULL, PRIMARY KEY(actor,key));
CREATE VIRTUAL TABLE IF NOT EXISTS blocks USING fts5(doc UNINDEXED,sec UNINDEXED,title,heading,body);
CREATE INDEX IF NOT EXISTS edits_revision ON edits(doc,rev);
CREATE TABLE IF NOT EXISTS restricted_pages(doc TEXT PRIMARY KEY REFERENCES pages(id));
CREATE TABLE IF NOT EXISTS page_grants(doc TEXT NOT NULL REFERENCES restricted_pages(doc),
 principal TEXT NOT NULL, may_edit INTEGER NOT NULL CHECK(may_edit IN (0,1)), PRIMARY KEY(doc,principal));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, doc TEXT NOT NULL REFERENCES pages(id),
 sec INTEGER NOT NULL, rev INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, source TEXT,
 at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE INDEX IF NOT EXISTS audit_document ON audit(doc, rev);
CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, doc TEXT NOT NULL,
 sec INTEGER NOT NULL, base_revision INTEGER NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL,
 author TEXT NOT NULL, state TEXT NOT NULL, reviewer TEXT, published_revision INTEGER);
CREATE TABLE IF NOT EXISTS assets(doc TEXT PRIMARY KEY REFERENCES pages(id), filename TEXT NOT NULL,
 kind TEXT NOT NULL, sha256 TEXT NOT NULL, data BLOB NOT NULL, author TEXT NOT NULL,
 imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS imports(name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS lexical_mirror(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,watermark INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mirror(id INTEGER PRIMARY KEY CHECK(id=1),name TEXT NOT NULL,watermark INTEGER NOT NULL);
"""


@contextmanager
def connect(path=DEFAULT_DB, write=False):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


def add_page(db, doc, title, lang, sections, *, grants=None):
    if not SLUG.fullmatch(doc) or not sections:
        raise ValueError("Invalid document")
    db.execute("INSERT INTO pages VALUES(?,?,?,1)", (doc,title,lang))
    if grants is not None:
        restrict_page(db, doc, grants)
    for sec, (heading, body) in enumerate(sections, 1):
        db.execute("INSERT INTO edits VALUES(?,?,1,?,?,NULL)", (doc,sec,heading,body))
        db.execute("INSERT INTO blocks VALUES(?,?,?,?,?)", (doc,sec,title,heading,body))


def split_sections(body):
    # Keep all source text, grouping paragraphs into addressable passages.
    groups, current, length = [], [], 0
    for para in re.split(r"\n\s*\n", body):
        if length >= 1300:
            groups.append("\n\n".join(current))
            current, length = [], 0
        current.append(para)
        length += len(para)
    groups.append("\n\n".join(current))
    return [(g.splitlines()[0].lstrip("# ")[:85] or "Overview", g) for g in groups if g] or [("Overview", "")]


def import_public(path):
    verify_inputs()
    with connect(path, True) as db:
        if db.execute("SELECT 1 FROM imports WHERE name='enterprise-v1'").fetchone():
            return
        for doc, title, body in documents():
            add_page(db, doc, title, "en", split_sections(body))
        db.execute("INSERT INTO imports VALUES('enterprise-v1')")


def restrict_page(db, doc, grants):
    """Set an explicit allowlist; an empty mapping makes the page inaccessible."""
    db.execute("INSERT OR IGNORE INTO restricted_pages VALUES(?)", (doc,))
    db.execute("DELETE FROM page_grants WHERE doc=?", (doc,))
    db.executemany("INSERT INTO page_grants VALUES(?,?,?)",
                   [(doc, principal, int(may_edit)) for principal, may_edit in grants.items()])


def allowed(db, doc, principal, *, edit=False):
    access = db.execute("""SELECT g.may_edit FROM restricted_pages r
        LEFT JOIN page_grants g ON r.doc=g.doc AND g.principal=?
        WHERE r.doc=?""", (principal, doc)).fetchone()
    return access is None or access["may_edit"] is not None and (not edit or bool(access["may_edit"]))


SOURCE = re.compile(r"([A-Za-z0-9_-]+)@(\d+)#s(\d+)\Z")


def source_visible(db, source, principal):
    match = SOURCE.fullmatch(source or "")
    return bool(match and allowed(db, match[1], principal))


def page(db, doc, rev=None, section=None, *, principal=None):
    info = db.execute("SELECT * FROM pages WHERE id=?", (doc,)).fetchone()
    if not info or not allowed(db, doc, principal):
        raise ValueError("Unknown page")
    rev = info["rev"] if rev is None else rev
    if not 1 <= rev <= info["rev"]:
        raise ValueError("Unknown revision")
    params = [doc,rev]
    where = "doc=? AND rev<=?"
    if section is not None:
        where += " AND sec=?"
        params.append(section)
    blocks = db.execute(f"""SELECT sec,heading,body,source,MAX(rev) AS changed FROM edits
                            WHERE {where} GROUP BY sec ORDER BY sec""", params).fetchall()
    if not blocks:
        raise ValueError("Unknown section")
    sections = [dict(b) for b in blocks]
    for block in sections:
        if block["source"] and not source_visible(db, block["source"], principal):
            block["source"] = None
    return {"id":doc, "title":info["title"], "lang":info["lang"], "revision":rev,
            "url":f"/p/{doc}/v{rev}.html"+(f"#s{section}" if section is not None else ""), "sections":sections}


def search(db, query, limit=5, *, principal=None):
    expr = terms(query)
    if not expr:
        return []
    rows = db.execute("""SELECT doc, sec, blocks.title, heading, pages.rev AS revision, snippet(blocks,4,'','', ' … ',24) AS excerpt
        FROM blocks JOIN pages ON pages.id=blocks.doc WHERE blocks MATCH ?
        AND (NOT EXISTS(SELECT 1 FROM restricted_pages r WHERE r.doc=blocks.doc)
          OR EXISTS(SELECT 1 FROM page_grants g WHERE g.doc=blocks.doc AND g.principal=?))
        ORDER BY rank LIMIT ?""", (expr, principal, max(1,min(limit,10))))
    return [with_url(dict(r)) for r in rows]


def with_url(hit):
    hit["url"] = f'/p/{hit["doc"]}/v{hit["revision"]}.html#s{hit["sec"]}'
    return hit


def publish(db, *, doc, sec, base_revision, body, source, actor):
    if not body.strip() or len(body) > 12000:
        raise ValueError("Invalid body")
    if not allowed(db, doc, actor, edit=True):
        raise ValueError("Unknown page")
    old = page(db, doc, section=sec, principal=actor)
    if old["revision"] != base_revision:
        raise ValueError(f"Version conflict: current revision {old['revision']}")
    verify_source(db, source, actor)
    revision = base_revision + 1
    heading = old["sections"][0]["heading"]
    db.execute("UPDATE pages SET rev=? WHERE id=?", (revision, doc))
    db.execute("INSERT INTO edits VALUES(?,?,?,?,?,?)", (doc,sec,revision,heading,body,source))
    db.execute("DELETE FROM blocks WHERE doc=? AND sec=?", (doc,sec))
    db.execute("INSERT INTO blocks VALUES(?,?,?,?,?)", (doc,sec,old["title"],heading,body))
    db.execute("INSERT INTO audit(doc,sec,rev,actor,action,source) VALUES(?,?,?,?,?,?)",
               (doc, sec, revision, actor, "publish", source))
    return {"id":doc, "revision":revision, "section":sec,
            "url":f"/p/{doc}/v{revision}.html#s{sec}", "source":source}


def verify_source(db, source, principal):
    match = SOURCE.fullmatch(source)
    if not match:
        raise ValueError("Expected source in page@revision#sN format")
    page(db, match[1], int(match[2]), int(match[3]), principal=principal)


def replace(path, *, doc, sec, base_revision, body, source, request_id, role, principal="local-editor"):
    if role != "editor":
        raise ValueError("Reader cannot edit")
    if not 1 <= len(request_id) <= 128:
        raise ValueError("Invalid request ID")
    signature = hashlib.sha256(json.dumps([doc,sec,base_revision,body,source],
                                          ensure_ascii=False).encode()).hexdigest()
    with connect(path, True) as db:
        if not allowed(db, doc, principal, edit=True):
            raise ValueError("Unknown page")
        verify_source(db, source, principal)
        prior = db.execute("SELECT * FROM actor_requests WHERE actor=? AND key=?",
                           (principal, request_id)).fetchone()
        if not prior and principal == "local-editor":
            prior = db.execute("SELECT * FROM requests WHERE key=?", (request_id,)).fetchone()
        if prior:
            if prior["signature"] != signature:
                raise ValueError("Request ID reused with different arguments")
            return json.loads(prior["reply"])
        result = publish(db, doc=doc, sec=sec, base_revision=base_revision, body=body,
                         source=source, actor=principal)
        db.execute("INSERT INTO actor_requests VALUES(?,?,?,?)",
                   (principal, request_id, signature, json.dumps(result)))
        return result


def history(db, doc, principal=None):
    current = page(db, doc, principal=principal)["revision"]
    events = {}
    for row in db.execute("SELECT * FROM audit WHERE doc=? ORDER BY id", (doc,)):
        value = dict(row)
        if value["source"] and not source_visible(db, value["source"], principal):
            value["source"] = None
        events.setdefault(value["rev"], []).append(value)
    return [{"revision": v, "events": events.get(v, [])} for v in range(current, 0, -1)]
