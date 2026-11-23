"""Grow a clone of the frozen XERJ release with deterministic synthetic runbooks."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

from corpus import ROOT
from retrieval import XERJ, json_request, xerj_search
from scale_xerj import RUN, SHA256, save, upload
from stage1_benchmark import rank_score, stats

PROFILE = "synthetic-runbook-v1"
STEP_BYTES = 8_000_000
DISK_RESERVE = 8 * 1024**3
SYSTEMS = ("gateway", "scheduler", "queue", "cache", "ledger", "console", "replica", "collector",
           "router", "exporter", "bridge", "runner", "registry", "notifier", "catalog", "storage")
REGIONS = ("EU", "US", "APAC", "CA", "UK", "LATAM")
ACTIONS = (
    "Compare the previous approved limit with staging telemetry and record the agreed exception.",
    "Notify the service owner when retries continue after the recovery window.",
    "Preserve the last stable revision and record the rollout decision in the change log.",
    "Review the customer-facing procedure with the regional operations team before publication.",
    "Confirm the queue depth and recovery objective against the production dashboard.",
    "Read the escalation matrix before moving the change to the next deployment ring.",
    "Check the approved capacity range against the audit export for the current week.",
    "Document the observation and request an independent review of the runbook update.",
)
CHECKS = ("availability", "readiness", "failover", "retention", "routing", "capacity", "escalation", "recovery")


def runbook(number, remaining):
    rng = random.Random(20261122 + number)
    system = SYSTEMS[number % len(SYSTEMS)]
    region = rng.choice(REGIONS)
    project = f"talvik{number:07d}"
    title = f"{project} {region} {system} operations runbook"
    size = min(remaining, rng.randrange(16_384, 49_153))
    limit = rng.randrange(18, 144)
    head = (f"{title}\n\nApproved timeout\nThe approved {region} {system} timeout is {limit} seconds.\n"
            "Earlier values in the change record are historical.\n")
    entries = [head] if size >= len(head) else [head[:size]]
    count = len(entries[0])
    while count < size:
        kind = rng.choice(CHECKS)
        note = rng.choice(ACTIONS)
        owner = rng.randrange(10, 1950)
        group = rng.randrange(1, 25)
        checkpoint = rng.randrange(10_000, 99_999)
        line = (f"\n{kind.capitalize()} checkpoint {checkpoint} for {system} in {region}. "
                f"Owner group {owner}, rollout ring {group}. {note}\n")
        entries.append(line[:size-count])
        count += len(entries[-1])
    return f"synthetic:{project}", "".join(entries), f"What is the approved {system} timeout for {project} in {region}?"


@contextmanager
def measure(directory):
    start_free = shutil.disk_usage(directory).free
    resource = {"start_free_bytes": start_free, "min_free_bytes": start_free, "xerj_rss_bytes": 0,
                "generator_rss_bytes": 0, "interval_s": 2}
    done = threading.Event()
    pid = os.environ.get("WIKI_XERJ_PID")
    def monitor():
        while True:
            resource["min_free_bytes"] = min(resource["min_free_bytes"], shutil.disk_usage(directory).free)
            if pid:
                for key, child in (("xerj_rss_bytes", pid), ("generator_rss_bytes", str(os.getpid()))):
                    reading = subprocess.run(["ps", "-o", "rss=", "-p", child], capture_output=True, text=True)
                    if reading.stdout.strip().isdigit():
                        resource[key] = max(resource[key], int(reading.stdout.strip())*1024)
            if done.wait(2):
                return
    thread = threading.Thread(target=monitor)
    thread.start()
    try:
        yield resource
    finally:
        done.set()
        thread.join()
        resource["end_free_bytes"] = shutil.disk_usage(directory).free


def index_bytes(directory):
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def synthesize(index, first, remaining, *, cap=STEP_BYTES):
    batch, digest = [], hashlib.sha256()
    size = 0
    current = first
    while size < cap and size < remaining:
        key, text, _ = runbook(current, remaining-size)
        encoded = text.encode("ascii")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        digest.update(f"{key}\t{len(encoded)}\t{fingerprint}\n".encode("ascii"))
        batch.extend((json.dumps({"index": {"_index": index, "_id": key}}).encode(),
                      json.dumps({"content": text}).encode()))
        size += len(encoded)
        current += 1
    return batch, size, current, digest.hexdigest()


def verify_or_replay(index, state):
    expected = state["base_documents"] + state["added_documents"]
    json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
    observed = json_request(f"{XERJ}/{index}/_count")["count"]
    repair = {"expected_at_checkpoint": expected, "observed_after_refresh": observed, "replayed_batches": 0}
    if observed >= expected:
        return repair
    # A detached drive may lose the last acknowledged slices. Replay by stable ID,
    # verifying each generated payload against the saved manifest before upload.
    deficit = expected-observed
    replay_count = min(len(state["batches"]), max(16, deficit//180 + 4))
    if replay_count > 96:
        raise RuntimeError(f"Restored index is missing {deficit} documents; short replay would not be safe")
    for part in state["batches"][-replay_count:]:
        batch, sz, ending, sha = synthesize(index, part["first"], part["bytes"], cap=part["bytes"])
        if (sz, ending, sha) != (part["bytes"], part["last"]+1, part["sha256"]):
            raise ValueError("Recovered batch differs from the saved manifest")
        upload(batch)
    json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
    recovered = json_request(f"{XERJ}/{index}/_count")["count"]
    if recovered < expected:
        raise RuntimeError(f"Replayed index still has {recovered} documents. Checkpoint kept.")
    return {**repair, "replayed_batches": replay_count, "observed_after_replay": recovered}


def index_growth(data_dir, checkpoint, target, previous=None):
    base = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    original = json.loads((RUN/"checkpoint.json").read_text())
    index = original["index"]
    directory = data_dir / index
    if directory.resolve() == (ROOT/".state/xerj"/index).resolve() or not directory.is_dir():
        raise ValueError("Use an isolated clone of the release index")
    if base["archive_sha256"] != SHA256 or original["archive_sha256"] != SHA256:
        raise ValueError("Release fingerprints differ")
    original_bytes = base["utf8_bytes"]
    original_docs = base["documents"]
    if target <= original_bytes:
        raise ValueError("Growth target must exceed the release size")
    metadata = {"index": index, "archive_sha256": SHA256, "target_bytes": target,
                "base_bytes": original_bytes, "base_documents": original_docs, "profile": PROFILE}
    restart = checkpoint.exists()
    if restart:
        state = json.loads(checkpoint.read_text())
        if any(state.get(key) != value for key, value in metadata.items()):
            raise ValueError("Checkpoint parameters changed")
    else:
        earlier = {"added_bytes": 0, "added_documents": 0, "batches": []}
        if previous is not None:
            earlier = json.loads(previous.read_text())
            if (any(earlier.get(k) != v for k, v in metadata.items() if k != "target_bytes")
                    or earlier["base_bytes"]+earlier["added_bytes"] != earlier["target_bytes"]
                    or earlier["target_bytes"] >= target):
                raise ValueError("The previous checkpoint is not a completed compatible scale")
        actual = json_request(f"{XERJ}/{index}/_count")["count"]
        expected = original_docs + earlier["added_documents"]
        if actual != expected:
            raise ValueError(f"Expected clone with {expected} documents, got {actual}")
        state = {**metadata, "added_bytes": earlier["added_bytes"], "added_documents": earlier["added_documents"],
                 "batches": earlier["batches"], "continued_from_bytes": original_bytes+earlier["added_bytes"],
                 "initial_index_bytes": index_bytes(directory)}
        save(checkpoint, state)

    added_target = target - original_bytes
    wall = time.perf_counter()
    with measure(data_dir) as resource:
        if restart:
            recovery = verify_or_replay(index, state)
            state.setdefault("resume_checks", []).append(recovery)
            print(json.dumps(recovery), flush=True)
            save(checkpoint, state)
        while state["added_bytes"] < added_target:
            chunk_start = time.perf_counter()
            if shutil.disk_usage(data_dir).free < DISK_RESERVE:
                raise RuntimeError("Disk reserve reached. The last acknowledged checkpoint is intact.")
            first = state["added_documents"]
            batch, batch_bytes, num, digest = synthesize(index, first, added_target-state["added_bytes"])
            generated = time.perf_counter()
            upload(batch)
            finished = time.perf_counter()
            state["added_bytes"] += batch_bytes
            state["added_documents"] = num
            state["batches"].append({"first": first, "last": num - 1, "bytes": batch_bytes,
                                     "sha256": digest, "generate_s": generated-chunk_start,
                                     "upload_s": finished-generated})
            save(checkpoint, state)
            if len(state["batches"]) % 16 == 0 or state["added_bytes"] == added_target:
                print(f'{original_bytes+state["added_bytes"]:,}/{target:,} bytes, '
                      f'{num:,} new documents, {finished-wall:.1f}s', flush=True)

        timer = time.perf_counter()
        json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
        refresh_s = time.perf_counter()-timer
        count = json_request(f"{XERJ}/{index}/_count")["count"]
        if count != state["added_documents"] + original_docs:
            raise RuntimeError(f"Expected {state['added_documents'] + original_docs} documents, got {count}")
        final_bytes = index_bytes(directory)

    return state, {"documents": count, "utf8_bytes": target, "logical_index_bytes_after_refresh": final_bytes,
                   "import_wall_s_current_invocation": time.perf_counter()-wall, "refresh_s": refresh_s,
                   "generate_s_all_acks": sum(x["generate_s"] for x in state["batches"]),
                   "upload_s_all_acks": sum(x["upload_s"] for x in state["batches"]),
                   "resource_samples": resource}


def evaluate(state, output=None, payload=None):
    release = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    en = [r for r in release["rows"] if r["lang"] == "en"]
    index = state["index"]
    tasks = [("release", r["id"], r["query"], r["gold"]) for r in en]
    n = state["added_documents"]
    for offset in sorted({i*n//16 for i in range(16)}):
        key, body, question = runbook(offset, 50_000)
        stored = json_request(f"{XERJ}/{index}/_doc/{quote(key, safe='')}")["_source"]["content"]
        if not body.startswith(stored):
            raise ValueError("Generated reference document differs from the indexed text")
        tasks.append(("synthetic", key, question, [key]))
    rows = []
    for group, key, query, gold in tasks:
        started = time.perf_counter()
        hits = xerj_search(index, query, limit=5)
        rows.append({"group": group, "id": key, "query": query, "gold": gold, "hits": hits,
                     "ms": (time.perf_counter()-started)*1000, **rank_score(hits, gold)})
        if output is not None and len(rows) % 8 == 0:
            save(output, {**payload, "search_progress": {"completed": len(rows), "expected": len(tasks),
                                                          "rows": rows}})
            print(f"Measured {len(rows)}/{len(tasks)} XERJ searches", flush=True)
    summary = {}
    for group in ("release", "synthetic"):
        group_rows = [row for row in rows if row["group"] == group]
        summary[group] = {"queries": len(group_rows), "hit5": sum(row["hit5"] for row in group_rows),
                          "recall5": sum(row["recall5"] for row in group_rows)/len(group_rows),
                          **stats(row["ms"] for row in group_rows)}
    return {"transport": "localhost HTTP, one sequential pass", "summary": summary, "rows": rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--target-bytes", type=int, default=10_000_000_000)
    p.add_argument("--continue-from", type=Path)
    args = p.parse_args()
    with args.checkpoint.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state, result = index_growth(args.data_dir, args.checkpoint, args.target_bytes, args.continue_from)
        payload = {"profile": PROFILE, "source": state, "indexing": result}
        save(args.output, payload)
        payload["search"] = evaluate(state, args.output, payload)
        save(args.output, payload)


if __name__ == "__main__":
    main()
