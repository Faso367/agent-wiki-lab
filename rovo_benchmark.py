"""Compare XERJ to the official Rovo Search tool on the frozen wiki benchmark."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import time
from urllib.parse import urlsplit

from atlassian_client import Atlassian
from corpus import ROOT, STATE
from retry_cloud_search import local_index, read_manifest
from scale_xerj import SHA256, save, verify_archive
from stage1_benchmark import local_call, local_session, rank_score, stats

BASELINE = ROOT / "bench-questions.json"
BASELINE_HASH = "d57627625d2afbb184e4f519c331b4392055bc421b54d01e780b42dd636a2257"
SITE = os.environ.get("WIKI_ATLASSIAN_SITE", "example.atlassian.net")
SPACE = os.environ.get("WIKI_ATLASSIAN_SPACE", "DEMO")
SPACE_ID = os.environ.get("WIKI_ATLASSIAN_SPACE_ID", "0")
SEED = 20261122
TIMEOUT = 90
CONTROL = "Emergent Tenant Behavior Simulation and Operator Playbook"


def tasks():
    raw = BASELINE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != BASELINE_HASH:
        raise ValueError("The frozen question set has changed")
    return [{k: row[k] for k in ("id", "query", "gold")} for row in json.loads(raw)]


def response_data(envelope):
    if envelope.get("isError"):
        message = json.dumps(envelope).lower()
        if "credit" in message and any(w in message for w in ("insufficient", "quota", "exhaust", "available")):
            raise RuntimeError("rovo_credit_limit")
        if "429" in message or "rate limit" in message:
            raise RuntimeError("rate_limited")
        raise RuntimeError("mcp_tool_error")
    if envelope.get("structuredContent") is not None:
        return envelope["structuredContent"]
    return json.loads(envelope["content"][0]["text"])


class Rovo:
    def __init__(self, socket, inventory):
        self.cloud = Atlassian(socket, SITE, SPACE, SPACE_ID)
        self.pages = {cloud: source for source, cloud in inventory["pages"].items()}

    async def search(self, query, *, snippets=False, depth=5):
        envelope = await self.cloud.envelope("search", {
            "cloudId": SITE, "query": query, "targetApp": "CONFLUENCE", "limit": 10,
        })
        return self.positions(response_data(envelope)["results"][:depth], snippets=snippets)

    def positions(self, results, *, snippets=False):
        positions = []
        for hit in results:
            kind = hit.get("type", "unknown")
            link = urlsplit(hit.get("url") or "")
            prefix = f"/wiki/spaces/{SPACE}/pages/"
            page = link.path.removeprefix(prefix).partition("/")[0]
            source = self.pages.get(page) if (
                link.hostname == SITE and link.path.startswith(prefix)
                and str(hit.get("id", "")).endswith(f":page/{page}")
            ) else None
            position = {"type": kind, "id": source}
            if snippets and source is not None:
                from pilot_evidence import SNIPPET_CHARS
                position["snippet"] = (hit.get("snippet") or "")[:SNIPPET_CHARS]
            positions.append(position)
        return positions


async def measured(operation, query, gold):
    start = time.perf_counter()
    try:
        async with asyncio.timeout(TIMEOUT):
            positions = await operation(query)
        return {"ms": (time.perf_counter() - start) * 1000, "positions": positions,
                **rank_score([hit["id"] for hit in positions], gold)}
    except (TimeoutError, OSError, KeyError, ValueError, RuntimeError, IndexError) as exc:
        error = str(exc) if str(exc) in ("rovo_credit_limit", "rate_limited", "mcp_tool_error") else type(exc).__name__
        return {"ms": (time.perf_counter() - start) * 1000, "error": error, **rank_score([], gold)}


def summarize(rows):
    report = {}
    for arm in ("xerj", "rovo"):
        values = [row["arms"][arm] for row in rows if arm in row["arms"]]
        good = [v for v in values if "error" not in v]
        bad = [v for v in values if "error" in v]
        if values:
            report[arm] = {"attempts": len(values), "hit5": sum(v["hit5"] for v in values),
                           "recall5": statistics.mean(v["recall5"] for v in values),
                           "errors": dict(Counter(v["error"] for v in bad)),
                           "outside_or_unmapped": sum(h["id"] is None for v in good for h in v["positions"]),
                           "positions": sum(len(v["positions"]) for v in good),
                           "successful_calls": stats(v["ms"] for v in good),
                           "failed_calls": stats(v["ms"] for v in bad)}
    pairs = [r["arms"] for r in rows if len(r["arms"]) == 2]
    if pairs:
        delta = [int(p["xerj"]["hit5"]) - int(p["rovo"]["hit5"]) for p in pairs]
        rng = random.Random(73)
        means = sorted(statistics.mean(rng.choices(delta, k=len(delta))) for _ in range(10_000))
        report["paired"] = {"attempted": len(pairs), "without_error": sum(
            all("error" not in v for v in pair.values()) for pair in pairs),
            "xerj_only": delta.count(1), "rovo_only": delta.count(-1),
            "hit5_xerj_minus_rovo_pp": 100 * statistics.mean(delta),
            "hit5_delta_ci95_pp": [100 * means[i] for i in (250, 9749)]}
    return report


async def benchmark(args):
    verify_archive()
    questions = tasks()
    random.Random(SEED).shuffle(questions)
    inventory = json.loads(args.inventory.read_text())
    manifest = read_manifest(args.manifest)
    expected = {row["xerj_id"] for row in manifest.values()}
    gold = {doc for q in questions for doc in q["gold"]}
    if (not inventory["verified"] or inventory.get("archive_sha256") != SHA256 or inventory["site"] != SITE or inventory["space"] != SPACE
            or set(inventory["pages"]) != expected or len(expected) != 512
            or set(inventory["gold_reads"]) != gold
            or any(str(row["space_id"]) != SPACE_ID for row in inventory["gold_reads"].values())):
        raise ValueError("The verified Confluence corpus does not match the local manifest")
    rovo = Rovo(args.socket, inventory)
    index = "rovo-paired-512"
    count = local_index(index, manifest)
    result = {"corpus_pages": count, "questions": len(questions), "scope": {
        "xerj": "512 documents", "rovo": "site Confluence, no server-side space filter",
    }, "settings": {"questions_sha256": BASELINE_HASH, "shuffle_seed": SEED, "timeout_s": TIMEOUT,
                    "parallelism": 1, "rovo_limit": 10, "scored_depth": 5}, "rows": []}
    async with local_session(args.output.parent / "benchmark.sqlite", index=index) as local:
        async def xerj(query):
            ids = await local_call(local, "search_corpus", query=query)
            return [{"type": "page", "id": key} for key in ids]

        await xerj(CONTROL)
        calls = {"xerj": xerj, "rovo": rovo.search}
        for ordinal, task in enumerate(questions):
            order = list(calls)[::-1] if ordinal % 2 else list(calls)
            row = {**task, "order": order, "arms": {}}
            for arm in order:
                row["arms"][arm] = await measured(calls[arm], task["query"], task["gold"])
            result["rows"].append(row)
            save(args.output, result)
            if row["arms"]["rovo"].get("error") == "rovo_credit_limit":
                result["stop_reason"] = "rovo_credit_limit"
                break
            if (ordinal + 1) % 8 == 0:
                print(f"Measured {ordinal + 1}/{len(questions)} pairs", flush=True)
    result["summary"] = summarize(result["rows"])
    save(args.output, result)
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=STATE / "retry-cloud-nested/pages.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a fresh output path")
    asyncio.run(benchmark(args))
