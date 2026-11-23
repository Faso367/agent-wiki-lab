"""Index the pinned full EnterpriseRAG-Bench release and evaluate paired wiki queries."""
import fcntl
import hashlib
import json
import random
import re
import shutil
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from corpus import DOWNLOADS, ROOT, STATE, documents, questions, verify_inputs
from fetch_assets import DATA, digest, fetch
from retrieval import XERJ, json_request, xerj_search

ARCHIVE = DOWNLOADS / "all_documents.zip"
SHA256 = "9d1174928696ad08bc15f3f104739519de633c1605a4ec2034e0e3c0087bc5cd"
RUN = STATE / "full-release-lexical-v1"


def verify_archive():
    if digest(ARCHIVE) != SHA256:
        raise ValueError("Corpus archive checksum differs from the frozen release")


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def upload(batch):
    payload = b"\n".join(batch) + b"\n"
    reply = json_request(f"{XERJ}/_bulk", payload, ndjson=True)
    if reply.get("errors") or len(reply["items"]) * 2 != len(batch):
        raise RuntimeError(f"Bulk import failed: {str(reply)[:500]}")


def index_release():
    state_file = RUN / "checkpoint.json"
    with ZipFile(ARCHIVE) as archive:
        files = [f for f in archive.infolist() if not f.is_dir() and f.filename != "questions.jsonl"]
        ids = [f.filename.split("/", 1)[0] + ":" + Path(f.filename).name.partition("__")[0] for f in files]
        if any(not re.fullmatch(r"[a-z_]+:dsid_[0-9a-f]{32}", key) for key in ids):
            raise ValueError("Unexpected document in release")
        repeated = {key for key, count in Counter(ids).items() if count > 1}
        ids = [key + ":" + hashlib.sha256(f.filename.encode()).hexdigest() if key in repeated else key
               for key, f in zip(ids, files)]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate document IDs")
        if state_file.exists():
            state = json.loads(state_file.read_text())
            if state["archive_sha256"] != SHA256:
                raise ValueError("Checkpoint belongs to another release")
        else:
            index = "enterprise-full-" + uuid.uuid4().hex[:12]
            json_request(f"{XERJ}/{index}", {"mappings": {"properties": {
                "content": {"type": "text"}}}}, method="PUT")
            state = dict(index=index, archive_sha256=SHA256, committed=0)
            save(state_file, state)
        index = state["index"]
        batch = []
        size = 0
        for i in range(state["committed"], len(files)):
            raw = archive.read(files[i])
            batch.append(json.dumps({"index": {"_index": index, "_id": ids[i]}}).encode())
            batch.append(json.dumps({"content": raw.decode("utf-8")}, ensure_ascii=False).encode())
            size += len(raw)
            if size >= 2_000_000 or len(batch) >= 1024 or i + 1 == len(files):
                if shutil.disk_usage(STATE).free < 8 * 1024**3:
                    raise RuntimeError("Less than 8 GiB free. Import checkpoint retained.")
                upload(batch)
                if (i + 1) // 25000 > state["committed"] // 25000:
                    print(f"Indexed {i + 1:,}/{len(files):,}", flush=True)
                state["committed"] = i + 1
                save(state_file, state)
                batch.clear()
                size = 0
        json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
        count = json_request(f"{XERJ}/{index}/_count")["count"]
        if count != len(files):
            raise ValueError(f"Index returned {count}, expected {len(files)}")
        sources = Counter(f.filename.split("/", 1)[0] for f in files)
        return dict(index=index, documents=count, utf8_bytes=sum(f.file_size for f in files),
                    archive_sha256=SHA256, sources=dict(sources))


def evaluate(info):
    ru = dict(line.split("\t", 1) for line in (ROOT / "queries-ru.tsv").read_text().splitlines())
    wiki_ids = {key for key, _, _ in documents()}
    tasks = []
    for q in questions(wiki_ids):
        key = q["question_id"]
        gold = ["confluence:" + doc for doc in q["expected_doc_ids"]]
        tasks.append((key, "en", q["question"], gold))
        if key in ru:
            tasks.append((key, "ru", ru[key], gold))
    random.Random(42).shuffle(tasks)
    rows = []
    for key, lang, query, gold in tasks:
        start = time.perf_counter()
        hits = xerj_search(info["index"], query, limit=5)
        rows.append(dict(id=key, lang=lang, query=query, gold=gold, hits=hits,
                         ms=round((time.perf_counter() - start) * 1000, 2)))
    summary = {}
    for lang in ("en", "ru"):
        group = [r for r in rows if r["lang"] == lang]
        timings = sorted(r["ms"] for r in group)
        summary[lang] = dict(queries=len(group),
                             hit5=sum(bool(set(r["hits"]) & set(r["gold"])) for r in group),
                             p50_ms=statistics.median(timings),
                             p95_ms=timings[(95 * len(group) + 99) // 100 - 1])
    save(RUN / "results.json", dict(**info, summary=summary, rows=rows))
    print(json.dumps(dict(**info, summary=summary), indent=2))


def main():
    RUN.mkdir(exist_ok=True)
    with (RUN / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_inputs()
        fetch(DATA + ARCHIVE.name, ARCHIVE, SHA256)
        evaluate(index_release())


if __name__ == "__main__":
    main()
