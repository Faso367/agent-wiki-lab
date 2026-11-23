"""Rebuildable XERJ mirror over the authoritative SQLite sections."""
import json
from pathlib import Path
import re
import uuid
from urllib.error import HTTPError

from retrieval import XERJ, json_request
from wiki import allowed, connect, with_url


class LexicalSearch:
    def __init__(self, path):
        self.path = Path(path)

    def _index(self, db, rebuild=False):
        entry = db.execute("SELECT name,watermark FROM lexical_mirror WHERE id=1").fetchone()
        if entry and not rebuild:
            name, watermark = entry
            try:
                json_request(f"{XERJ}/{name}/_count")
                return name, watermark
            except HTTPError as exc:
                if exc.code != 404:
                    raise
        name = "wiki-sections-" + uuid.uuid4().hex
        json_request(f"{XERJ}/{name}", {"mappings": {"properties": {
            "content": {"type": "text"}}}}, method="PUT")
        return name, 0

    def _sync(self, db, rebuild=False):
        name, watermark = self._index(db, rebuild)
        newest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM edits").fetchone()[0]
        if newest > watermark:
            rows = db.execute("""SELECT e.doc,e.sec,p.title,e.heading,e.body,MAX(e.rev)
                FROM edits e JOIN pages p ON e.doc=p.id
                WHERE e.rowid>? GROUP BY e.doc,e.sec ORDER BY e.doc,e.sec""", (watermark,))
            while batch := rows.fetchmany(128):
                entries = []
                for row in batch:
                    entries.append(json.dumps({"index": {"_id": f'{row["doc"]}:{row["sec"]}'}}))
                    entries.append(json.dumps({"content": "\n".join((row["title"], row["heading"], row["body"]))},
                                              ensure_ascii=False))
                data = ("\n".join(entries) + "\n").encode()
                ack = json_request(f"{XERJ}/{name}/_bulk", data, ndjson=True)
                if ack.get("errors") or len(ack["items"]) != len(batch):
                    raise RuntimeError("XERJ rejected a section batch")
            json_request(f"{XERJ}/{name}/_refresh", {}, method="POST")
        db.execute("INSERT OR REPLACE INTO lexical_mirror VALUES(1,?,?)", (name, newest))
        return name

    def rebuild(self):
        with connect(self.path, True) as db:
            name = self._sync(db, rebuild=True)
            return {"index": name, "sections": json_request(f"{XERJ}/{name}/_count")["count"]}

    def search(self, query, limit, principal=None):
        if not query.strip():
            return []
        limit = max(1, min(10, limit))
        with connect(self.path, True) as db:
            name = self._sync(db)
            hits = json_request(f"{XERJ}/{name}/_search", {"size": 100, "_source": False,
                                                           "query": {"match": {"content": query}}})
            results = []
            for hit in hits["hits"]["hits"]:
                doc, sec = hit["_id"].rsplit(":", 1)
                if not allowed(db, doc, principal):
                    continue
                row = db.execute("""SELECT doc,sec,blocks.title,heading,pages.rev AS revision,body
                    FROM blocks JOIN pages ON pages.id=blocks.doc WHERE doc=? AND sec=?""",
                    (doc, int(sec))).fetchone()
                if row:
                    item = dict(row)
                    item["excerpt"] = excerpt(item.pop("body"), query)
                    results.append(with_url(item))
                    if len(results) == limit:
                        break
            return results


def excerpt(body, query):
    words = sorted(set(re.findall(r"[^\W_]{3,}", query)), key=len, reverse=True)
    for word in words[:40]:
        match = re.search(r"\b" + re.escape(word) + r"\b", body, re.I)
        if match:
            start = max(0, match.start()-60)
            return ("… " if start else "") + body[start:start+240]
    return body[:240]
