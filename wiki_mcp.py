"""Run from the experiment directory. Role and DB path are chosen by the local operator."""
import argparse
from functools import lru_cache
from pathlib import Path
from typing import Any
from corpus import ROOT
from materials import source_asset
from review import decide, propose, propose_page, read_draft
from mcp.server.fastmcp import FastMCP
from wiki import DEFAULT_DB, allowed, connect, history, initialize, page, replace, search


def serve(path, role, public_index=None, engine="sqlite", corpus_index=None, principal=None):
    principal = principal or "local-" + role
    initialize(path)
    server = FastMCP("Local team wiki")

    if engine == "lexical":
        from lexical_wiki import LexicalSearch
        mirror = LexicalSearch(path)
    elif engine == "xerj":
        from live_search import LiveSearch
        mirror = LiveSearch()

    @server.tool()
    def search_sections(query: str, limit: int = 5) -> list:
        """Search current sections by a question or keywords. Returns page IDs, section numbers and short excerpts."""
        if engine == "lexical":
            return mirror.search(query, limit, principal)
        if engine == "xerj":
            return mirror.search(path, query, limit, principal)
        with connect(path) as db:
            return search(db, query, limit, principal=principal)

    @server.tool()
    def read_section(doc: str, sec: int, revision: int | None = None) -> dict:
        """Read one section by page ID and number. Omit revision to read the latest version. Only request a specific revision for an explicit historical lookup. Copy the returned url for citation."""
        with connect(path) as db:
            entry = page(db, doc, revision, sec, principal=principal)
            if material := source_asset(db, doc, principal):
                entry["material"] = material
            return entry

    @server.tool()
    def outline(doc: str) -> dict:
        """List section numbers and headings of a page without loading its body."""
        with connect(path) as db:
            result = page(db,doc,principal=principal)
        result["sections"] = [{"sec":b["sec"],"heading":b["heading"]} for b in result["sections"]]
        return result

    if corpus_index:
        from retrieval import xerj_search

        @server.tool()
        def search_corpus(query: str, limit: int = 5) -> list[str]:
            """Search the frozen benchmark corpus and return original document IDs."""
            return xerj_search(corpus_index, query, limit=max(1, min(limit, 10)))

    if public_index:
        from retrieval import xerj_search
        from embedding import Encoder
        get_encoder = lru_cache(maxsize=1)(Encoder)

        @server.tool()
        def discover_originals(query: str) -> list:
            """Find original imported English wiki pages via multilingual public snapshot. Read the current version with read_section."""
            ids = xerj_search(public_index,query,get_encoder())
            with connect(path) as db:
                return [dict(r) for doc in ids if allowed(db, doc, principal) for r in db.execute(
                    "SELECT id,title,rev AS current_revision FROM pages WHERE id=?", (doc,))]

    @server.tool()
    def page_history(doc: str) -> list[dict]:
        """List readable revisions and the recorded author, time and source."""
        with connect(path) as db:
            return history(db, doc, principal)

    if role == "editor":
        card = "ui://wiki/review.html"

        @server.resource(card, mime_type="text/html;profile=mcp-app")
        def review_card() -> str:
            return (ROOT / "review_card.html").read_text()

        @server.tool(meta={"ui": {"resourceUri": card}})
        def show_draft(draft_id: str) -> dict[str, Any]:
            """Open a proposed change with old text, new text and source."""
            with connect(path) as db:
                return read_draft(db, draft_id, principal, role)

        @server.tool()
        def decide_draft(draft_id: str, decision: str) -> dict[str, Any]:
            """Approve or reject a pending draft on behalf of the authenticated editor."""
            return decide(path, draft_id, decision, actor=principal, role=role)

        @server.tool()
        def propose_section(doc: str, sec: int, revision: int, body: str, source: str) -> dict:
            """Propose replacement text for one section, with linked section evidence."""
            return propose(path, doc=doc, sec=sec, revision=revision, body=body, source=source,
                           actor=principal, role=role)

        @server.tool()
        def propose_article(doc: str, title: str, lang: str, sections: list[tuple[str, str]], source: str) -> dict:
            """Propose a new unpublished article. Each section is [heading, body]."""
            return propose_page(path, doc=doc, title=title, lang=lang, sections=sections,
                                source=source, actor=principal, role=role)

        @server.tool()
        def update_section(doc: str, sec: int, base_revision: int, body: str,
                           source: str, request_id: str) -> dict:
            """Replace exactly one section. Provide a source page@revision#sN and unique retry-safe request ID."""
            return replace(path,doc=doc,sec=sec,base_revision=base_revision,body=body,
                           source=source,request_id=request_id,role=role,principal=principal)

    server.run(transport="stdio")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    p.add_argument("--role",choices=["reader","editor"],default="reader")
    p.add_argument("--public-index")
    p.add_argument("--engine",choices=["sqlite","xerj","lexical"],default="sqlite")
    p.add_argument("--corpus-index")
    p.add_argument("--principal", help="Operator-assigned identity for this stdio session")
    args = p.parse_args()
    serve(args.db,args.role,args.public_index,args.engine,args.corpus_index,args.principal)
