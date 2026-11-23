"""Local concurrency, restoration and capacity measurements on explicitly named data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

from corpus import ROOT, STATE
from lexical_wiki import LexicalSearch
from retrieval import XERJ, json_request, xerj_search
from scale_xerj import RUN, save
from stage1_benchmark import stats
from wiki import add_page, connect, history, initialize, page, replace


def measured(func, iterable, workers):
    def execute(value):
        start = time.perf_counter()
        try:
            record = {"value": func(value)}
        except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
            record = {"error": str(exc)}
        record["elapsed_ms"] = (time.perf_counter()-start)*1000
        return record
    start = time.perf_counter()
    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(execute, iterable))
    total = (time.perf_counter()-start)*1000
    return {"workers": workers, "n": len(rows), "wall_ms": total,
            "errors": sum("error" in row for row in rows), "rows": rows, **stats(r["elapsed_ms"] for r in rows)}


def measurement_path(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def snapshot(path, copy):
    with sqlite3.connect(path) as source, sqlite3.connect(copy) as dest:
        source.backup(dest)
        check = dest.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        raise ValueError("SQLite backup integrity check failed")


def run(index_dir):
    release = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    index = json.loads((RUN/"checkpoint.json").read_text())["index"]
    documents = json_request(f"{XERJ}/{index}/_count")["count"]
    if documents != release["documents"] or release["index"] != index:
        raise ValueError("Frozen release and running XERJ index differ")
    workload = [r["query"] for r in release["rows"] if r["lang"] == "en"][:24]
    directory = index_dir / index
    index_size = measurement_path(directory)
    start_free = shutil.disk_usage(index_dir).free
    rss = []
    pid = os.getenv("WIKI_XERJ_PID")
    def read_rss():
        if pid:
            size = subprocess.check_output(["ps", "-o", "rss=", "-p", pid], text=True)
            rss.append(int(size.strip())*1024)
    read_rss()
    def run_query(query):
        return xerj_search(index, query)

    for query in workload:
        xerj_search(index, query)
    orders = ((1, 4, 8), (4, 8, 1), (8, 1, 4))
    http_reads = {str(workers): [] for workers in orders[0]}
    for order in orders:
        for workers in order:
            http_reads[str(workers)].append(measured(run_query, workload, workers))
            read_rss()
    with tempfile.TemporaryDirectory(dir=STATE) as temp:
        db = Path(temp)/"wiki.sqlite"
        backup = Path(temp)/"backup.sqlite"
        restored = Path(temp)/"restore.sqlite"
        initialize(db)
        with connect(db, True) as tx:
            add_page(tx, "rule", "Accepted policy", "en", [("Decision", "Use cobalt readiness check.")])
            for i in range(8):
                add_page(tx, f"guide-{i}", "Release guide", "en", [("Rule", "Use initial check."),
                        ("Escalation", "Notify on-call.")])
        live = LexicalSearch(db)
        old = live.rebuild()
        def mixed(i):
            if i % 3 == 0:
                doc = f"guide-{(i//3)%8}"
                return replace(db, doc=doc, sec=1, base_revision=1, body="Use cobalt readiness check.",
                               source="rule@1#s1", request_id=str(i), role="editor", principal=f"editor-{i%2}")
            return [hit["doc"] for hit in live.search("readiness release", 3)]
        workload_result = measured(mixed, range(24), 4)
        start = time.perf_counter()
        snapshot(db, backup)
        snapshot(backup, restored)
        restore_ms = (time.perf_counter()-start)*1000
        start = time.perf_counter()
        rebuild = LexicalSearch(restored).rebuild()
        reindex_ms = (time.perf_counter()-start)*1000
        with connect(restored) as tx:
            changes = [entry for i in range(8) for entry in history(tx, f"guide-{i}")[0]["events"]]
            identical = all(page(tx, f"guide-{i}")["revision"] == 2 for i in range(8))
        found = LexicalSearch(restored).search("cobalt readiness", 10)
        recovered = (identical and rebuild["sections"] == old["sections"] == 17 and len(changes) == 8
                     and all(e["source"] == "rule@1#s1" for e in changes)
                     and {h["doc"] for h in found} >= {"rule", *(f"guide-{i}" for i in range(8))})
        if not recovered:
            raise ValueError("Restored content, audit or search differed")
        recovery = {"backup_bytes": backup.stat().st_size, "restore_ms": restore_ms, "reindex_ms": reindex_ms,
                    "sections": rebuild["sections"], "audit_events": changes, "verified": recovered}
    read_rss()
    return {"full_release": {"archive_sha256": release["archive_sha256"], "documents": documents,
                             "utf8_bytes": release["utf8_bytes"], "index_bytes": index_size,
                             "initial_free_disk_bytes": start_free,
                             "end_free_disk_bytes": shutil.disk_usage(index_dir).free,
                             "observed_xerj_max_rss_bytes": max(rss, default=None),
                             "reads_http": http_reads, "read_order": orders, "warmup_queries": len(workload)},
            "editing_fixture": {"documents": 9, "mixed": workload_result, "recovery": recovery},
            "confluence": {"10_gb_run": False, "40_to_50_gb_run": False},
            "capacity_linear_projection": {str(size): round(index_size*size*10**9/release["utf8_bytes"])
                                           for size in (10, 40, 50)}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    save(args.output, run(args.index_dir))
