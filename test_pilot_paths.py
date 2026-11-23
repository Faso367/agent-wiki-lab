"""Regression tests for the shared HTML/MCP permission and publication paths."""
import asyncio
from base64 import b64encode
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from corpus import ROOT
from lexical_wiki import LexicalSearch
from materials import import_file, source_asset
from retrieval import LOCAL, XERJ, json_request
from review import decide, propose, propose_page
from site_view import build_server
from wiki import add_page, connect, history, initialize, page, replace, restrict_page, search


class PilotPaths(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.db = self.folder/"wiki.sqlite"
        initialize(self.db)
        with connect(self.db, True) as tx:
            add_page(tx, "source", "Gateway decision", "en", [("Accepted", "Use 45 seconds.")])
            add_page(tx, "guide", "Gateway guide", "en", [("Timeout", "Use 90 seconds."),
                      ("Escalation", "Tell on-call.")])
            add_page(tx, "private", "Hidden title avory", "en", [("Hidden", "marker obsidianlarch")],
                      grants={"alice": True, "charlie": False})
        self.identities = {name: {"role": role, "token": f"test-only-{name}"}
                          for name, role in (("alice", "editor"), ("bob", "editor"), ("charlie", "reader"))}
        self.server = build_server(self.db, 0, identities=self.identities)
        self.root = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, route, actor=None, data=None):
        headers = {}
        if actor:
            value = b64encode(f'{actor}:{self.identities[actor]["token"]}'.encode()).decode()
            headers["Authorization"] = f"Basic {value}"
        if data:
            headers["Origin"] = self.root
        try:
            result = LOCAL.open(Request(self.root+route, data, headers), timeout=5)
        except HTTPError as exc:
            result = exc
        with result:
            return result.status, result.read(), result.headers

    def test_html_read_and_review(self):
        first = propose(self.db, doc="guide", sec=1, revision=1, body="Use 45 seconds.",
                        source="source@1#s1", actor="alice", role="editor")
        stale = propose(self.db, doc="guide", sec=1, revision=1, body="Use 100 seconds.",
                        source="source@1#s1", actor="alice", role="editor")
        url = first["url"]
        self.assertEqual(self.request(url)[0], 404)
        self.assertIn(b"Use 90 seconds", self.request(url, "bob")[1])
        self.assertNotIn(b'value="approve"', self.request(url, "alice")[1])
        self.assertEqual(self.request(url, "bob", b"decision=approve")[0], 200)
        self.assertEqual(self.request(stale["url"], "bob", b"decision=approve")[0], 409)
        result = decide(self.db, first["id"], "approve", actor="bob", role="editor")
        self.assertEqual(result["revision"], 2)
        rejected = decide(self.db, stale["id"], "reject", actor="bob", role="editor")
        self.assertEqual(rejected["state"], "rejected")
        self.assertIn(b"Use 45 seconds", self.request("/p/guide/index.html")[1])
        self.assertIn(b"Use 90 seconds", self.request("/p/guide/v1.html#s1")[1])
        self.assertIn(b'+Use 45 seconds', self.request("/history/guide/2")[1])
        self.assertIn(b"/p/source/v1.html#s1", self.request("/history/guide")[1])
        with connect(self.db) as tx:
            edited = page(tx, "guide")
            self.assertEqual(edited["sections"][1]["body"], "Tell on-call.")
            self.assertEqual(history(tx, "guide")[0]["events"][0]["actor"], "bob")

    def test_create_and_permission_revocation(self):
        new = propose_page(self.db, doc="new-guide", title="New guide", lang="en",
                           sections=[("Rule", "Only use the accepted limit.")],
                           source="source@1#s1", actor="alice", role="editor")
        self.assertEqual(self.request("/p/new-guide/index.html")[0], 404)
        self.assertEqual(decide(self.db, new["id"], "approve", actor="bob", role="editor")["revision"], 1)
        self.assertIn(b"source/v1.html#s1", self.request("/p/new-guide/index.html")[1])
        options = dict(doc="guide", sec=1, base_revision=1, body="Approved from evidence.",
                       source="private@1#s1", request_id="same", role="editor", principal="alice")
        self.assertEqual(replace(self.db, **options), replace(self.db, **options))
        self.assertEqual(self.request("/p/private/v1.html", "charlie")[0], 200)
        self.assertNotIn(b"private/v1.html", self.request("/p/guide/v2.html", "bob")[1])
        with connect(self.db, True) as tx:
            restrict_page(tx, "private", {"bob": True})
        self.assertEqual(self.request("/p/private/v1.html", "charlie")[0], 404)
        self.assertEqual(self.request("/history/private", "charlie")[0], 404)
        self.assertNotIn(b"Hidden title", self.request("/?q=obsidianlarch", "charlie")[1])
        self.assertEqual(self.request("/p/private/v1.html", "bob")[2]["Cache-Control"], "no-store, private")
        with self.assertRaisesRegex(ValueError, "Unknown page"):
            replace(self.db, **options)

    def test_materials_and_acl(self):
        samples = [
            ("gpt.md", b"# Gateway report\n\nThe ultramarine marker is verified.", "gpt-report", "ultramarine"),
            ("claude.html", b"<h1>Artifact</h1><p>bluemarker</p><script>stealsecret()</script>",
             "claude-artifact", "bluemarker"),
            ("plain.txt", b"readiness bandleader", "document", "bandleader"),
        ]
        if os.getenv("WIKI_QA_PDF"):
            samples.append(("control.pdf", Path(os.environ["WIKI_QA_PDF"]).read_bytes(),
                            "document", "ceruleananchor"))
        results = []
        for n, (name, original, kind, word) in enumerate(samples):
            doc = f"asset-{n}"
            asset = self.folder/name
            asset.write_bytes(original)
            started = time.perf_counter()
            imported = import_file(self.db, asset, doc=doc, title=name, lang="en", kind=kind,
                                   author="alice", grants={"alice": True})
            if os.getenv("WIKI_QA_XERJ"):
                matching = LexicalSearch(self.db).search(word, 5, "alice")
                visible_ms = (time.perf_counter()-started)*1000
                self.assertEqual(matching[0]["doc"], doc)
                self.assertEqual(LexicalSearch(self.db).search(word, 5), [])
                results.append({**imported, "filename": name, "kind": kind,
                                "import_to_search_ms": visible_ms})
            self.assertEqual(imported["sha256"], hashlib.sha256(original).hexdigest())
            with connect(self.db) as tx:
                self.assertEqual(search(tx, word, principal="alice")[0]["doc"], doc)
                self.assertEqual(search(tx, word), [])
                self.assertIsNone(source_asset(tx, doc))
            self.assertEqual(self.request(f"/assets/{doc}/original")[0], 404)
            code, data, headers = self.request(f"/assets/{doc}/original", "alice")
            self.assertEqual((code, data), (200, original))
            self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
            preview = self.request(f"/p/{doc}/v1.html", "alice")[1]
            self.assertIn(word.encode(), preview)
            self.assertNotIn(b"stealsecret", preview)
            self.assertIn(f"/assets/{doc}/original".encode(), preview)
        if artifact := os.getenv("WIKI_QA_RESULTS"):
            Path(artifact).write_text(json.dumps(results, indent=2)+"\n")

    def test_stdio_acl_and_card_protocol(self):
        draft = propose(self.db, doc="guide", sec=1, revision=1, body="Use 45 seconds.",
                        source="source@1#s1", actor="alice", role="editor")
        async def run():
            for actor, role in (("bob", "editor"), ("charlie", "reader")):
                args = [str(ROOT/"wiki_mcp.py"), "--db", str(self.db), "--role", role, "--principal", actor]
                params = StdioServerParameters(command=sys.executable, args=args)
                async with stdio_client(params) as streams, ClientSession(*streams) as client:
                    await client.initialize()
                    result = await client.call_tool("read_section", {"doc": "private", "sec": 1})
                    self.assertEqual(result.isError, actor == "bob")
                    matching = await client.call_tool("search_sections", {"query": "obsidianlarch avory"})
                    shown = " ".join(piece.text for piece in matching.content)
                    self.assertEqual("Hidden title avory" in shown, actor == "charlie")
                    if role == "reader":
                        self.assertTrue((await client.call_tool("show_draft", {"draft_id": draft["id"]})).isError)
                        continue
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    uri = tools["show_draft"].meta["ui"]["resourceUri"]
                    resource = (await client.read_resource(uri)).contents[0]
                    self.assertIn("profile=mcp-app", resource.mimeType)
                    self.assertIn("ui/initialize", resource.text)
                    reveal = await client.call_tool("show_draft", {"draft_id": draft["id"]})
                    self.assertTrue(reveal.structuredContent["can_decide"], reveal)
                    result = await client.call_tool("decide_draft", {"draft_id": draft["id"], "decision": "approve"})
                    self.assertEqual(result.structuredContent["state"], "approved")
        asyncio.run(run())

    @unittest.skipUnless(os.getenv("WIKI_QA_XERJ"), "needs the experiment XERJ process")
    def test_lexical_consistency_and_missing_index_recovery(self):
        mirror = LexicalSearch(self.db)
        index = mirror.rebuild()["index"]
        self.assertEqual(mirror.search("obsidianlarch", 5), [])
        self.assertEqual(mirror.search("obsidianlarch", 5, "charlie")[0]["doc"], "private")
        case = replace(self.db, doc="guide", sec=1, base_revision=1, body="Use bluecedarlimit.",
                       source="source@1#s1", request_id="update", role="editor")
        self.assertEqual(mirror.search("bluecedarlimit", 5)[0]["url"], case["url"])
        with connect(self.db, True) as tx:
            restrict_page(tx, "private", {"alice": True})
        self.assertEqual(mirror.search("obsidianlarch", 5, "charlie"), [])
        proposed = propose_page(self.db, doc="new-article", title="Article", lang="en",
                                sections=[("Rule", "borealbirch")], source="source@1#s1",
                                actor="alice", role="editor")
        self.assertEqual(mirror.search("borealbirch", 5), [])
        decide(self.db, proposed["id"], "approve", actor="bob", role="editor")
        self.assertEqual(mirror.search("borealbirch", 5)[0]["doc"], "new-article")
        json_request(f"{XERJ}/{index}", method="DELETE")
        self.assertEqual(mirror.search("bluecedarlimit", 5)[0]["url"], case["url"])


if __name__ == "__main__":
    unittest.main()
