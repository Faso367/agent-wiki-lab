"""Build the paired CQL search workload and score captured Atlassian MCP responses."""
import argparse
import json
import math
import re
from pathlib import Path
from statistics import median

from corpus import ROOT
from scale_xerj import save

CAPTURE_FIELDS = ["results.content.id", "results.content.title", "cqlQuery", "searchDuration", "totalSize"]
ID_SUFFIX = re.compile(r" - ([a-z_]+)-(dsid_[0-9a-f]{32})(?:-([0-9a-f]{64}))?$")


def workload(space, site):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", space):
        raise ValueError("Invalid space key")
    local = json.loads((ROOT / "results/full-release-lexical.json").read_text())
    for item in local["rows"]:
        words = re.findall(r"[^\W_]+", item["query"], re.UNICODE)
        query = " OR ".join(dict.fromkeys(words))
        cql = f'space = "{space}" AND type = page AND text ~ "{query}"'
        yield {"id": item["id"], "lang": item["lang"], "cql": cql,
               "arguments": {"cloudId": site, "name": "searchConfluence",
                             "inputs": {"cql": cql, "limit": 5}, "responseFields": CAPTURE_FIELDS},
               "gold": item["gold"], "local_ms": item["ms"], "local_hits": item["hits"]}


def score(space, raw, output, site):
    tasks = list(workload(space, site))
    captures = [json.loads(line) for line in raw.read_text().splitlines()]
    if len(tasks) != len(captures):
        raise ValueError(f"Expected {len(tasks)} responses, received {len(captures)}")
    rows = []
    for task, data in zip(tasks, captures):
        if data["cqlQuery"] != task["cql"]:
            raise ValueError("Response did not match the expected query")
        ids = []
        for hit in data["results"]:
            match = ID_SUFFIX.search(hit["content"]["title"])
            ids.append(":".join(x for x in match.groups() if x) if match else None)
        rows.append({"id": task["id"], "lang": task["lang"], "cloud_hits": ids,
                     "cloud_hit5": bool(set(ids) & set(task["gold"])),
                     "local_hit5": bool(set(task["local_hits"]) & set(task["gold"])),
                     "cloud_reported_ms": data["searchDuration"], "local_http_ms": task["local_ms"]})
    summary = {}
    for lang in ("en", "ru"):
        group = [r for r in rows if r["lang"] == lang]
        times = sorted(r["cloud_reported_ms"] for r in group)
        summary[lang] = dict(n=len(group), cloud_hit5=sum(r["cloud_hit5"] for r in group),
                             xerj_hit5=sum(r["local_hit5"] for r in group),
                             cloud_reported_p50_ms=median(times),
                             cloud_reported_p95_ms=times[math.ceil(len(times)*.95)-1])
    save(output, dict(space=space, summary=summary, rows=rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("space")
    parser.add_argument("--site", required=True)
    parser.add_argument("--responses", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.responses:
        score(args.space, args.responses, args.output, args.site)
    else:
        save(args.output, list(workload(args.space, args.site)))
