"""Evaluate versioned, immutable public wiki discovery cards. Raw rows are the report input."""
import argparse
import json
import random
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path
from statistics import median
import numpy as np
from corpus import ROOT, STATE, card, documents, questions, verify_inputs
from embedding import Encoder
from retrieval import XERJ, bulk, create_index, json_request, terms, xerj_search


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=STATE / "retrieval.json")
    args = p.parse_args()
    verify_inputs()
    cards = {d: card(t, b) for d, t, b in documents()}
    data = np.load(STATE / "cards.npz")
    ids, vectors = data["ids"].tolist(), data["vectors"]
    if list(cards) != ids or vectors.shape != (len(ids), 384):
        raise ValueError("Embedding cache does not match corpus")
    encoder = Encoder()
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE cards USING fts5(doc UNINDEXED, content)")
    db.executemany("INSERT INTO cards VALUES(?,?)", cards.items())
    index = "wikibench-" + uuid.uuid4().hex[:12]
    create_index(index)
    for start in range(0, len(ids), 128):
        bulk(index, [(doc, cards[doc], vectors[i]) for i, doc in
                     enumerate(ids[start:start+128], start)])
    json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
    count = json_request(f"{XERJ}/{index}/_count")["count"]
    if count != len(ids):
        raise RuntimeError(f"Only {count} of {len(ids)} visible")
    ru = dict(l.split("\t", 1) for l in (ROOT/"queries-ru.tsv").read_text().splitlines())
    tasks = []
    for q in questions(set(ids)):
        key = q["question_id"]
        langs = [("en", q["question"])]
        if key in ru:
            langs.append(("ru", ru[key]))
        for lang, text in langs:
            for arm in ["SQLite BM25", "XERJ lexical", "XERJ + E5 RRF"]:
                tasks.append((key, lang, text, q["expected_doc_ids"], arm))
    if sum(t[1]=="ru" for t in tasks) != 3 * len(ru):
        raise ValueError("Russian queries without eligible originals")
    random.Random(42).shuffle(tasks)
    results = []
    for key, lang, query, gold, arm in tasks:
        start = time.perf_counter()
        if arm == "SQLite BM25":
            hits = [row[0] for row in db.execute(
                "SELECT doc FROM cards WHERE cards MATCH ? ORDER BY rank LIMIT 5", (terms(query),))]
        else:
            hits = xerj_search(index, query, encoder if arm.endswith("RRF") else None)
        ms = (time.perf_counter()-start)*1000
        results.append(dict(id=key, lang=lang, arm=arm, hits=hits, gold=gold,
                            hit=bool(set(hits)&set(gold)), recall=len(set(hits)&set(gold))/len(set(gold)),
                            ms=round(ms,3)))
    groups = defaultdict(list)
    for r in results:
        groups[r["arm"], r["lang"]].append(r)
        if r["lang"] == "en" and r["id"] in ru:
            groups[r["arm"], "en-paired"].append(r)
    summary = []
    for (arm, lang), rows in sorted(groups.items()):
        ts = [r["ms"] for r in rows]
        summary.append(dict(arm=arm, lang=lang, n=len(rows), hit5=sum(r["hit"] for r in rows),
                            recall5=round(sum(r["recall"] for r in rows)/len(rows),3),
                            p50_ms=round(median(ts),2), p95_ms=round(float(np.percentile(ts,95)),2)))
    args.output.write_text(json.dumps(dict(documents=count, index=index, summary=summary, rows=results),
                                      ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
