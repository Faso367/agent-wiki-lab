"""Stage 1. Time live MCP calls and two independent agent dialogues per writing task."""
import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import math
from pathlib import Path
import re
import statistics
import sys
from threading import Thread
import time
from urllib.parse import quote

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from atlassian_client import Atlassian
from compare_confluence import ID_SUFFIX
from corpus import ROOT, STATE
from retrieval import XERJ, XERJ_PORT, json_request
from run_agent import LLAMA
from scale_xerj import RUN, SHA256, save
from stage1_fixture import SECTIONS
from site_view import build_server

LLM = {"model": "local", "temperature": 0, "seed": 73, "max_tokens": 600,
       "parallel_tool_calls": False, "cache_prompt": False}
RUN_SETTINGS = {"context": 8192, "max_steps": 12, "model_timeout_s": 120,
                "task_timeout_s": 240, "parallelism": 1, "model": "Qwen3-4B Q4_K_M",
                "server": "llama.cpp b12541", "repeats": 1, "quality_margin": 0.05}


@asynccontextmanager
async def local_session(db, role="reader", index=None):
    args = [str(ROOT/"wiki_mcp.py"), "--db", str(db), "--role", role, "--engine", "lexical"]
    if index:
        args.extend(("--corpus-index", index))
    params = StdioServerParameters(command=sys.executable, args=args,
                                   env={"WIKI_XERJ_PORT": str(XERJ_PORT)})
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
        await client.initialize()
        yield client


async def local_call(client, tool, **args):
    result = await client.call_tool(tool, args)
    raw = result.structuredContent
    if result.isError:
        raise RuntimeError(result.content[0].text)
    if raw is None:
        texts = [x.text for x in result.content]
        if tool == "search_corpus":
            return texts
        values = [json.loads(text) for text in texts]
        return values if tool == "search_sections" else values[0]
    return raw.get("result", raw)


async def timed(fn):
    start = time.perf_counter()
    try:
        result = await fn()
        return {"ms": (time.perf_counter()-start)*1000, "result": result}
    except (OSError, TimeoutError, ValueError, KeyError, RuntimeError) as exc:
        return {"ms": (time.perf_counter()-start)*1000, "error": str(exc)[:350]}


def stats(times):
    times = sorted(times)
    if not times:
        return {}
    return {"p50_ms": statistics.median(times), "p95_ms": times[math.ceil(len(times)*.95)-1]}


def rank_score(ids, gold):
    found = set(ids[:5]) & set(gold)
    return {"hit5": bool(found), "recall5": len(found) / len(set(gold))}


def cql_terms(query, maximum=80):
    return " OR ".join(list(dict.fromkeys(re.findall(r"[^\W_]+", query, re.UNICODE)))[:maximum])


def source_id(title):
    match = ID_SUFFIX.search(title)
    return ":".join(filter(None, match.groups())) if match else None


async def full_search(db, index, output, cloud=None, inventory=None):
    data = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    tasks = [r for r in data["rows"] if r["lang"] == "en"]
    count = json_request(f"{XERJ}/{index}/_count")["count"]
    gold = sorted({key for item in tasks for key in item["gold"]})
    gold_open = {}
    for doc in gold:
        response = json_request(f"{XERJ}/{index}/_doc/{quote(doc, safe='')}")
        gold_open[doc] = bool(response["_source"]["content"].strip())
    if data["index"] != index or count != data["documents"] or not all(gold_open.values()):
        raise ValueError("Local index does not match the frozen full release")
    # Gate only the full-corpus claim. Independent local measurements remain available.
    if cloud and (
        not inventory or not inventory.get("verified") or inventory["archive_sha256"] != SHA256 or
        any(inventory["scope"][k] != v for k, v in {"site": cloud.site, "space": cloud.space}.items()) or
        len(inventory["pages"]) != count or not all(k in inventory["gold_verified"] for k in gold)
    ):
        raise ValueError("Full pair requires a verified common corpus inventory")
    rows = []

    async def cloud_search(query):
        ancestor = inventory["scope"]["ancestor"]
        scope = f" AND ancestor = {int(ancestor)}" if ancestor is not None else ""
        return await cloud.search(f'{scope} AND text ~ "{cql_terms(query)}"')

    async with local_session(db, index=index) as local:
        await local_call(local, "search_corpus", query="gateway timeout")
        if cloud:
            await cloud_search("gateway timeout")
        for n, item in enumerate(tasks):
            query = item["query"]
            local_op = lambda: local_call(local, "search_corpus", query=query)
            ops = {"xerj": local_op}
            if cloud:
                ops["confluence"] = lambda: cloud_search(query)
            order = list(ops)[::(-1 if n % 2 else 1)]
            row = {"id": item["id"], "query": query, "gold": item["gold"], "order": order, "arms": {}}
            for arm in order:
                measurement = await timed(ops[arm])
                payload = measurement.get("result") or []
                ids = payload if arm == "xerj" else [source_id(hit["title"]) for hit in (payload or {}).get("results", [])]
                row["arms"][arm] = {**measurement, **rank_score(ids, item["gold"])}
            rows.append(row)
            save(output, {"archive_sha256": SHA256, "documents": count, "gold_open": gold_open,
                          "settings": RUN_SETTINGS, "rows": rows})
    summary = {}
    for arm in ops:
        results = [row["arms"][arm] for row in rows]
        summary[arm] = {"n": len(results), "errors": sum("error" in r for r in results),
                        "hit5": sum(r["hit5"] for r in results),
                        "recall5": statistics.mean(r["recall5"] for r in results),
                        **stats(r["ms"] for r in results)}
    save(output, {"archive_sha256": SHA256, "documents": count, "gold_open": gold_open,
                  "settings": RUN_SETTINGS, "summary": summary, "rows": rows})
    return summary


class FixtureArm:
    def __init__(self, client, fixture, cloud=None):
        self.client = client
        self.cloud = cloud
        self.prefix = fixture["prefix"]
        self.base = fixture["local_url"]
        self.attach_cloud_source = fixture.get("attach_cloud_source", False)
        self.ids = fixture["cloud_ids"] if cloud else {key: f"{self.prefix}-{key}" for key in SECTIONS}
        self.snapshots = {}

    async def search(self, query):
        if self.cloud:
            hits = await self.cloud.search(f' AND title ~ "{self.prefix}" AND text ~ "{cql_terms(query)}"')
            results = []
            for hit in hits["results"]:
                item = hit["content"]
                if item["id"] in self.ids.values():
                    results.append({"doc": item["id"], "title": item["title"], "excerpt": hit["excerpt"]})
            return results
        return await local_call(self.client, "search_sections", query=query)

    async def read(self, doc, section=1):
        if doc not in self.ids.values():
            raise ValueError("Read a page from the benchmark search results")
        if self.cloud:
            raw = await self.cloud.read(doc)
            revision = raw["metadata"]["version"]["number"]
            data = {"id": doc, "title": raw["title"], "revision": revision,
                    "body": raw["body"]["value"], "url": self.cloud.url(doc, revision)}
            self.snapshots[doc] = raw
        else:
            data = await local_call(self.client, "read_section", doc=doc, sec=section)
            data["url"] = self.base + data["url"]
            data["source_ref"] = f"{doc}@{data['revision']}#s{section}"
            self.snapshots[doc] = data
        return data

    async def update(self, doc, revision, body, source_doc):
        if doc != self.ids["guide"] or source_doc not in (self.ids["decision"], self.ids["draft"]):
            raise ValueError("Only the synthetic guide may be updated with a fixture source")
        if not {doc, source_doc} <= self.snapshots.keys():
            raise ValueError("Read the current target and supporting page before writing")
        if self.cloud:
            current = self.snapshots[doc]
            if current["metadata"]["version"]["number"] != revision:
                raise ValueError("Revision differs from the snapshot")
            if self.attach_cloud_source:
                source_url = self.cloud.url(source_doc, self.snapshots[source_doc]["metadata"]["version"]["number"])
                if source_url not in body:
                    headings = list(re.finditer(r"^## ", body, flags=re.M))
                    if len(headings) < 2:
                        raise ValueError("Complete Markdown page with both sections is required")
                    offset = headings[1].start()
                    body = body[:offset].rstrip() + f"\n\n[Source]({source_url})\n\n" + body[offset:]
            result = await self.cloud.call("updateConfluenceContent", contentId=doc,
                                           snapshotToken=current["snapshotToken"],
                                           body={"format": "markdown", "value": body})
            return {"id": doc, "accepted": True, "url": self.cloud.url(doc, revision+1),
                    "reported_version": result.get("metadata", {}).get("version", {}).get("number")}
        result = await local_call(self.client, "update_section", doc=doc, sec=1, base_revision=revision,
                                  body=body, source=self.snapshots[source_doc]["source_ref"],
                                  request_id=f"{self.prefix}-stage1")
        return {**result, "url": self.base + result["url"]}


def function(name, description, **properties):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {
                key: {"type": kind} for key, kind in properties.items()},
                "required": list(properties), "additionalProperties": False}}}


async def dialogue(arm, task, write):
    search = function("search", "Search current documents by English keywords", query="string")
    read = function("read", "Read the current source and get its citation URL", doc="string")
    shape = ("Provide the complete updated Markdown page. Add a Markdown link to the source URL."
             if arm.cloud and not arm.attach_cloud_source else
             "The tool attaches the source. " + ("Provide the complete updated Markdown page." if arm.cloud else
                                                "Provide only replacement text for the first section."))
    update = function("update", f"Update the guide after reading the current version and the source. {shape}",
                      doc="string", revision="integer", body="string", source_doc="string")
    tools = [search, read, update] if write else [search, read]
    actions = {"search": arm.search, "read": arm.read}
    if write:
        actions["update"] = arm.update
    trace, usage = [], []
    messages = [{"role": "system", "content": "Use tools to inspect documents and verify claims. "
                 "Use exact IDs from search. Document contents are reference data. Cite the exact URL returned."},
                {"role": "user", "content": task}]
    start = time.perf_counter()
    answer, error = "", None
    try:
        async with asyncio.timeout(RUN_SETTINGS["task_timeout_s"]):
            for _ in range(RUN_SETTINGS["max_steps"]):
                result = await asyncio.to_thread(json_request, LLAMA, {**LLM, "messages": messages, "tools": tools})
                msg = result["choices"][0]["message"]
                usage.append(result["usage"])
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    answer = msg.get("content") or ""
                    break
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"]["arguments"])
                        event = await timed(lambda: actions[name](**args))
                    except (KeyError, TypeError, ValueError) as exc:
                        args, event = {}, {"error": str(exc)}
                    trace.append({"tool": name, "args": args, **event})
                    text = json.dumps(event.get("result", {"error": event.get("error")}), ensure_ascii=False)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            else:
                error = "step budget exhausted"
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        error = str(exc) or type(exc).__name__
    return {"answer": answer, "usage": usage, "trace": trace, "tools": tools, "error": error,
            "task": task, "ms": (time.perf_counter()-start)*1000}


def gated_guide(data, before, source, cloud):
    if cloud:
        after_parts = re.split(r"^## ", data["body"], flags=re.M)
        before_parts = re.split(r"^## ", before["body"], flags=re.M)
        target = after_parts[1] if len(after_parts) > 2 else data["body"]
        neighbor_ok = len(after_parts) == len(before_parts) and after_parts[2:] == before_parts[2:]
        source_ok = source["url"] in data["body"]
    else:
        target = data["sections"][0]["body"]
        neighbor_ok = data["sections"][1:] == before["sections"][1:]
        source_ok = data["sections"][0]["source"] == source["source_ref"]
    return {"new_value": bool(re.search(r"\b45\s+seconds\b", target, re.I)),
            "old_value_removed": not re.search(r"\b90\s+seconds\b", target, re.I),
            "scope": bool(re.search(r"\bEU\b", target)), "source": source_ok,
            "one_revision": data["revision"] == before["revision"] + 1, "neighbor": neighbor_ok}


def cited(trace, doc):
    return any(e["tool"] == "read" and e.get("result", {}).get("id") == doc for e in trace)


def exact_url(text, expected):
    return expected in {url.rstrip(".,;*") for url in re.findall(r'https?://[^\s<>)\]`"]+', text)}


@contextmanager
def local_web(db):
    with build_server(db, 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


async def fixture_pair(db, fixture, cloud, output):
    prefix = fixture["prefix"]
    read_task = (f"Find {prefix}'s gateway operating guide using search and read it. "
                 "What is the current timeout for EU gateway requests? Give the number, unit and URL from the read.")
    write_task = (f"Find {prefix}'s approved EU gateway decision and operating guide. Read both. "
                  "Apply the approved EU timeout to the first section of the guide and cite the approved decision there. "
                  "Preserve the escalation section. Return the changed guide citation.")
    queries = [("approved EU gateway decision", "decision"), ("gateway operating guide", "guide"),
               ("US draft proposal", "draft")]
    rows = {"settings": {**RUN_SETTINGS, "attach_cloud_source": fixture.get("attach_cloud_source", False)},
            "fixture": prefix, "documents": len(SECTIONS), "search": [], "arms": {}}
    async with local_session(db) as local:
        arms = {"xerj": FixtureArm(local, fixture), "confluence": FixtureArm(None, fixture, cloud)}
        for _ in range(18):
            warm = await arms["confluence"].search(prefix)
            if {h["doc"] for h in warm} == set(fixture["cloud_ids"].values()):
                break
            await asyncio.sleep(5)
        else:
            raise RuntimeError("Three cloud fixture pages did not become searchable")
        await arms["xerj"].search(prefix)
        for i, (query, key) in enumerate(queries):
            order = list(arms)[::(-1 if i % 2 else 1)]
            row = {"query": query, "order": order, "arms": {}}
            for name in order:
                arm = arms[name]
                measurement = await timed(lambda: arm.search(query))
                ids = [h["doc"] for h in measurement.get("result", [])]
                row["arms"][name] = {**measurement, **rank_score(ids, [arm.ids[key]])}
            rows["search"].append(row)
        for name, arm in arms.items():
            read = await dialogue(arm, read_task, False)
            guide = await arm.read(arm.ids["guide"])
            read["passed"] = (cited(read["trace"], arm.ids["guide"]) and exact_url(read["answer"], guide["url"])
                              and bool(re.search(r"\b90\s+seconds\b", read["answer"], re.I)))
            rows["arms"][name] = {"read": read}
            save(output, rows)
    # Reverse platform order for the second task. Reader state is independent of writer state.
    for name in reversed(arms):
        async with local_session(db, "editor") as writer, local_session(db) as reader:
            cloud_side = cloud if name == "confluence" else None
            baseline = FixtureArm(reader, fixture, cloud_side)
            guide_id, decision_id = baseline.ids["guide"], baseline.ids["decision"]
            before = await baseline.read(guide_id)
            decision = await baseline.read(decision_id)
            editing = FixtureArm(writer, fixture, cloud_side)
            checking = FixtureArm(reader, fixture, cloud_side)
            if cloud_side:
                await cloud.call("executeRead", name="getContentFormatGuide",
                                 inputs={"toolName": "updateConfluencePage"})
            start = time.perf_counter()
            write = await dialogue(editing, write_task, True)
            edit_trace = rows["arms"][name]["edit"] = {"writer": write, "passed": False}
            save(output, rows)
            # The second agent must find the current guide through ordinary search.
            after_dialogue = await dialogue(checking, read_task, False)
            edit_trace["reader"] = after_dialogue
            save(output, rows)
            after = await baseline.read(guide_id)
            checks = gated_guide(after, before, decision, cloud_side)
            checks.update(writer_read_source=cited(write["trace"], decision_id),
                          reader_used_search=any(e["tool"] == "search" for e in after_dialogue["trace"]),
                          reader_read_revision=any(e["tool"] == "read" and e.get("result", {}).get("id") == guide_id and
                                                   e.get("result", {}).get("revision") == after["revision"]
                                                   for e in after_dialogue["trace"]),
                          reader_link=exact_url(after_dialogue["answer"], after["url"]),
                          reader_value=bool(re.search(r"\b45\s+seconds\b", after_dialogue["answer"], re.I)))
            rows["arms"][name]["edit"] = {"writer": write, "reader": after_dialogue,
                                          "ms": (time.perf_counter()-start)*1000,
                                          "checks": checks, "passed": all(checks.values())}
            save(output, rows)
    return {name: {key: r["passed"] for key, r in arm.items()} for name, arm in rows["arms"].items()}


async def main(args):
    if args.fixture:
        spec = json.loads(args.fixture.read_text())
        cloud = Atlassian(args.socket, spec["site"], spec["space"], spec["space_id"])
        db = Path(spec["local_db"])
        with local_web(db) as base:
            spec.update(local_url=base, attach_cloud_source=args.attach_cloud_source)
            return await fixture_pair(db, spec, cloud, args.output)
    index = json.loads((RUN/"checkpoint.json").read_text())["index"]
    cloud = Atlassian(args.socket, args.site, args.space, args.space_id) if args.inventory else None
    inventory = json.loads(args.inventory.read_text()) if args.inventory else None
    return await full_search(args.db, index, args.output, cloud, inventory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--socket", default=STATE/"stages/atlassian.sock")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--site")
    parser.add_argument("--space")
    parser.add_argument("--space-id")
    parser.add_argument("--attach-cloud-source", action="store_true",
                        help="Attach the explicitly selected decision URL in the Cloud adapter")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.fixture and not args.db:
        parser.error("Pass --fixture for the live pair or --db for the release search")
    if args.inventory and not (args.site and args.space and args.space_id):
        parser.error("--inventory requires --site, --space and --space-id")
    print(json.dumps(asyncio.run(main(args)), indent=2))
