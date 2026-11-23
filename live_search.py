"""Incremental section mirror for the local XERJ arm. SQLite remains authoritative."""
import uuid
from corpus import card
from embedding import Encoder
from retrieval import XERJ, bulk, create_index, json_request, xerj_search
from wiki import allowed, connect, with_url


class LiveSearch:
    def __init__(self):
        self.encoder = Encoder()

    def search(self, path, query, limit, principal=None):
        # Serialize imports, writes and mirror reads for a consistent local MVP snapshot.
        with connect(path,write=True) as db:
            state = db.execute("SELECT name,watermark FROM mirror WHERE id=1").fetchone()
            if state is None:
                name = "wikilive-"+uuid.uuid4().hex
                create_index(name)
                db.execute("INSERT INTO mirror VALUES(1,?,0)",(name,))
                watermark = 0
            else:
                name,watermark = state
            newest = db.execute("SELECT COALESCE(MAX(rowid),0) FROM edits").fetchone()[0]
            if newest > watermark:
                changed = db.execute("""SELECT e.doc,e.sec,p.title,e.heading,e.body,MAX(e.rev)
                    FROM edits e JOIN pages p ON e.doc=p.id WHERE e.rowid>?
                    GROUP BY e.doc,e.sec ORDER BY e.doc,e.sec""",(watermark,))
                while batch := changed.fetchmany(16):
                    texts = [card(r["title"]+" "+r["heading"],r["body"]) for r in batch]
                    vectors = self.encoder.encode(texts,"passage: ")
                    bulk(name,[(f'{r["doc"]}:{r["sec"]}',t,v) for r,t,v in zip(batch,texts,vectors)])
                json_request(f"{XERJ}/{name}/_refresh",{},method="POST")
                db.execute("UPDATE mirror SET watermark=? WHERE id=1",(newest,))
            ids = xerj_search(name,query,self.encoder,max(1,min(10,limit)))
            results = []
            for key in ids:
                doc,section = key.rsplit(":",1)
                if not allowed(db, doc, principal):
                    continue
                row = db.execute("""SELECT doc,sec,blocks.title,heading,pages.rev AS revision,substr(body,1,240) AS excerpt
                                 FROM blocks JOIN pages ON pages.id=blocks.doc WHERE doc=? AND sec=?""",(doc,int(section))).fetchone()
                if row:
                    results.append(with_url(dict(row)))
            return results
