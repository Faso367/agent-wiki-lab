from json import dumps
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock

from pilot_evidence import CloudText, READ_CHARS, blocks, window
from rovo_benchmark import Rovo, SITE, SPACE
from rovo_read_pilot import attempt, blind, final_answer, score


class EvidenceTests(unittest.TestCase):
    def test_html_and_text_keep_table_and_list_together(self):
        body = "# Settings\nalpha\n\n## Required files\n| Name | Use |\n| a | setup |\n- b\n- c"
        parser = CloudText()
        parser.feed("<p># Settings<br>alpha</p><p>## Required files<br>| Name | Use |<br>| a | setup |<br>- b<br>- c</p>")
        self.assertEqual(blocks(body), blocks("".join(parser.parts)))
        parts = blocks(body)
        chosen, _ = window(parts, "which files are required")
        self.assertIn("- b\n- c", parts[chosen.start])
        self.assertIn("| a | setup |", parts[chosen.start])

    def test_every_block_reachable_and_query_can_change_focus(self):
        body = ("x" * (READ_CHARS + 11)) + "\n\nA unique marker\n- line two\n- line three"
        parts = blocks(body)
        cursor, seen = 0, []
        while cursor is not None:
            chosen, nav = window(parts, "x", cursor)
            seen.extend(chosen)
            self.assertLessEqual(len("\n".join(parts[i] for i in chosen)), READ_CHARS)
            cursor = nav["next_cursor"]
        self.assertEqual(seen, list(range(len(parts))))
        self.assertEqual(window(parts, "unique marker")[0].start, 2)
        for cursor in (-1, len(parts), True):
            with self.assertRaises(ValueError):
                window(parts, "x", cursor)

    def test_search_does_not_show_external_snippets(self):
        client = Rovo(Path("unused"), {"pages": {"d": "11"}})
        hit = {"id": "ari:page/11", "type": "page", "url": f"https://{SITE}/wiki/spaces/{SPACE}/pages/11", "snippet": "details"}
        unknown = dict(hit, url=hit["url"].replace(SPACE, "PRIVATE"), snippet="outside")
        self.assertEqual(client.positions([unknown, hit], snippets=True),
                         [{"type": "page", "id": None}, {"type": "page", "id": "d", "snippet": "details"}])
        self.assertNotIn("snippet", client.positions([hit])[0])

    def test_final_rejects_missing_and_unknown_ids(self):
        evidence = {"S1": {"url": "known", "text": "A"}}
        for source in (None, [], "S2"):
            claim = {"part": 1, "text": "A", "quote": "A", "source": source}
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "cite_read_ids"):
                final_answer({"claims": [claim], "missing": []}, evidence, parts=["fact"])


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, steps, limit=5):
        issued = []
        def post(url, json):
            name, args = steps[len(issued)]
            if not issued and name == "search":
                args = {"parts": ["requested fact"], **args}
            if name == "finish":
                args = {**args, "claims": [{"part": 1, **c} for c in args["claims"]]}
            call = (name, args)
            issued.append([x["function"]["name"] for x in json["tools"]])
            message = {"role": "assistant", "tool_calls": [{"id": "1", "function": {
                "name": call[0], "arguments": dumps(call[1])}}]}
            return Mock(json=lambda: {"choices": [{"message": message}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        client = Mock(post=AsyncMock(side_effect=post))
        sources = Mock(search=AsyncMock(return_value=[{"doc": "d", "title": "D", "snippet": "x"}]),
                       read=AsyncMock(return_value={"doc": "d", "references": [{"text": "A", "block": 1, "url": "https://example.org/a"}]}))
        return await attempt(client, None, sources, "rovo", {"query": "q"}, limit), issued

    async def test_full_budget_plus_final_and_rejection(self):
        steps = [("search", {"query": str(i)}) for i in range(5)]
        steps += [("search", {"query": "sixth"})] + [("read", {"doc": "d"})] * 5
        steps += [("finish", {"claims": [{"text": "A", "source": "S1", "quote": "A"}], "missing": []})]
        result, issued = await self.drive(steps)
        self.assertIsNone(result["error"])
        self.assertEqual(result["calls"], {"search": 5, "read": 5})
        self.assertEqual(len(result["cited"]), 1)
        self.assertEqual(result["trace"][5]["result"]["error"], "tool_budget")
        self.assertNotIn("read", issued[-1])

    async def test_early_stop_and_bad_citation_retry(self):
        answer = lambda source: ("finish", {"claims": [{"text": "A", "source": source, "quote": "A"}], "missing": []})
        result, _ = await self.drive([("search", {"query": "a"}), ("read", {"doc": "d"}), answer("S9"), answer("S1")])
        self.assertEqual(result["trace"][2]["result"]["error"], "cite_read_ids")
        self.assertEqual(result["calls"], {"search": 1, "read": 1})
        self.assertEqual(result["answer"], "A [S1](https://example.org/a)")

    async def test_partial_answer_can_finish_after_one_search(self):
        partial = ("finish", {"claims": [], "missing": [1]})
        result, _ = await self.drive([("search", {"query": "a"}), partial], 2)
        self.assertEqual(result["calls"], {"search": 1})
        self.assertIn("Unanswered part 1: requested fact", result["answer"])

    async def test_bad_first_search_cannot_unlock_a_partial_submission(self):
        partial = ("finish", {"claims": [], "missing": [1]})
        result, issued = await self.drive([
            ("search", {"query": "bad", "page": 2}), partial,
            ("search", {"query": "good", "parts": ["actual request"]}), partial,
        ])
        self.assertEqual(issued[1], ["search"])
        self.assertEqual(result["trace"][1]["result"]["error"], "declare_requested_parts_on_first_search")
        self.assertEqual(result["requested_parts"], ["actual request"])
        self.assertEqual(result["calls"], {"search": 1})
        self.assertIsNone(result["error"])

    async def test_deadlines_include_failures_and_require_complete_pairs(self):
        partial = ("finish", {"claims": [], "missing": [1]})
        result, _ = await self.drive([("search", {"query": "a"}), partial], 1)
        rows = {"q": {"xerj": dict(result, ms=30000), "rovo": dict(result, ms=29999, error="TimeoutError")}}
        _, key = blind(rows, ["q"])
        # Historical scores can be loaded independently of the current submission format.
        summary = score(rows, key, {k: {"pass": v["arm"] == "xerj"} for k, v in key.items()})
        self.assertEqual(summary["xerj"]["correct_by_deadline"]["30"], {"passed": 1, "attempts": 1})
        self.assertEqual(summary["rovo"]["correct_by_deadline"]["30"], {"passed": 0, "attempts": 1})
        with self.assertRaises(ValueError):
            score(rows, key, {}, ["q", "absent"])


class CorpusRegression(unittest.TestCase):
    def test_reported_omissions_are_in_first_window_of_target_pages(self):
        from zipfile import ZipFile
        from corpus import STATE
        from retry_cloud_search import read_manifest
        from rovo_read_pilot import freeze
        from scale_xerj import ARCHIVE

        manifest_path = STATE / "retry-cloud-nested/pages.csv"
        if not ARCHIVE.exists() or not manifest_path.exists() or not (STATE / "downloads/questions.jsonl").exists():
            self.skipTest("requires the downloaded paired corpus")
        manifest = {row["xerj_id"]: row for row in read_manifest(manifest_path).values()}
        tasks = freeze()
        with ZipFile(ARCHIVE) as archive:
            for task, facts in (("qst_0150", ["inference:invoke", "metrics:read"]),
                                ("qst_0259", ["manifest.json", "manifest.sig", "README.txt", "break-glass"])):
                with self.subTest(task=task):
                    q = tasks[task]
                    parts = blocks(archive.read(manifest[q["gold"][0]]["archive_path"]).decode())
                    chosen, _ = window(parts, q["query"])
                    first_read = "\n".join(parts[i] for i in chosen)
                    for fact in facts:
                        self.assertIn(fact, first_read)


class ReviewRegression(unittest.TestCase):
    def test_ambiguous_cloud_reference_and_model_authored_links(self):
        from pilot_evidence import anchor_phrase
        part = "Repeated title"
        self.assertIsNone(anchor_phrase(part, "repeated title repeated title"))
        self.assertEqual(anchor_phrase(part + "\nUnique detail", "repeated title unique detail repeated title"), "Unique detail")
        for text in ("Fact [extra](https://invalid.test)", "Fact [S999]", "See <https://invalid.test>"):
            with self.assertRaisesRegex(ValueError, "put_citations_in_sources"):
                final_answer({"claims": [{"text": text, "source": "S1", "quote": "A"}], "missing": []}, {"S1": {"url": "real"}}, parts=["fact"])

    def test_urls_remain_literal_inside_commands(self):
        from rovo_read_pilot import claim_text
        text = "Run `curl http://localhost/status` and consult https://example.org/help"
        self.assertEqual(claim_text(text), "Run `curl http://localhost/status` and consult `https://example.org/help`")

    def test_512_documents_with_wrong_id_fail_before_reads(self):
        from unittest.mock import patch
        from rovo_read_pilot import verify_index
        manifest = {str(i): {"xerj_id": str(i)} for i in range(512)}
        hits = [{"_id": str(i + 1), "_source": {"content": "article"}} for i in range(512)]
        with patch("rovo_read_pilot.json_request", return_value={"hits": {"total": {"value": 512}, "hits": hits}}):
            with self.assertRaisesRegex(ValueError, "document IDs differ"):
                verify_index("test", manifest)


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_each_part_is_accounted_for_and_quote_must_match(self):
        evidence = {"S1": {"text": "The gate opens at 10:00.", "url": "u", "doc": "a"}}
        claim = {"part": 1, "text": "It opens at 10:00.", "source": "S1", "quote": "opens at 10:00."}
        args = {"claims": [claim], "missing": []}
        text = final_answer(args, evidence, parts=["opening time", "owner"])
        self.assertIn("Unanswered part 2: owner", text)
        self.assertIn("Partially answered part 1", final_answer({**args, "missing": [1]}, evidence, parts=["time"]))
        with self.assertRaisesRegex(ValueError, "use_requested_part_number"):
            final_answer({**args, "missing": ["other claim"]}, evidence, parts=["time"])
        for changes, error in [({"quote": "opens at 11:00"}, "copy_exact_quote"),
                               ({"part": True}, "use_requested_part_number"),
                               ({"text": "Use `X-Other`"}, "copy_literal_from_quote")]:
            with self.assertRaisesRegex(ValueError, error):
                final_answer({**args, "claims": [{**claim, **changes}]}, evidence, parts=["time"])
        self.assertIn("At least 1 distinct", final_answer({**args, "claims": [claim, claim]}, evidence, parts=["time"], document_count=True))

    async def test_cache_across_pages_and_deduplication(self):
        calls = [
            ("search", {"parts": ["topic"], "query": "topic"}),
            ("search", {"query": "topic", "page": 2}),
            ("search", {"query": "topic"}),
            ("finish", {"claims": [], "missing": [1]}),
        ]
        search = AsyncMock(return_value=[{"doc": str(i), "title": "title", "snippet": "text"}
                                         if i else None for i in [1, 2, 0, 3, 4, 1, 5, 5, 0, 6]])
        def post(url, json):
            name, args = calls.pop(0)
            reply = {"id": "op", "function": {"name": name, "arguments": dumps(args)}}
            return Mock(json=lambda: {"choices": [{"message": {"role": "assistant", "tool_calls": [reply]}}], "usage": {}})
        from time import perf_counter
        before = perf_counter()
        x = await attempt(Mock(post=AsyncMock(side_effect=post)), None, Mock(search=search), "xerj", {"query": "q"}, 1)
        elapsed_ms = 1000 * (perf_counter() - before)
        self.assertGreaterEqual(x["ms"], 0)
        self.assertLessEqual(x["ms"], elapsed_ms)
        self.assertIsNone(x["error"])
        self.assertEqual(search.await_count, 1)
        self.assertEqual(x["calls"]["search"], 1)
        self.assertEqual([r["doc"] for r in x["trace"][1]["result"]], ["5", "6"])
        self.assertEqual([c["cache_hit"] for c in x["trace"][:3]], [False, True, True])
