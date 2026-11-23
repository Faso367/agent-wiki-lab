"""Inventory an imported batch and compare identical search collections over MCP."""
import argparse
from collections import Counter
import asyncio
import csv
import json
from pathlib import Path
import re
import statistics
from urllib.parse import parse_qs, quote, urlsplit
from zipfile import ZipFile

from atlassian_client import Atlassian
from corpus import ROOT, STATE
from materials import VisibleText
from retrieval import XERJ, json_request
from scale_xerj import ARCHIVE, SHA256, save, upload
from stage1_benchmark import cql_terms, local_call, local_session, rank_score, stats, timed


def read_manifest(path):
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    result = {row["page_title"]: row for row in rows}
    if len(result) != len(rows) or len({row["xerj_id"] for row in rows}) != len(rows):
        raise ValueError("Import manifest titles and IDs must be unique")
    return result


def normalize(text):
    text = re.sub(r"(?m)^\s*#{1,6}[ \t]+", "", text).replace("\u00ad", "")
    return " ".join(text.split())


def next_cursor(batch):
    value = batch.get("nextCursor") or batch.get("cursor") or batch.get("_links", {}).get("next")
    if value and (value.startswith("/") or value.startswith("https://")):
        value = parse_qs(urlsplit(value).query).get("cursor", [None])[0]
    return value


async def census(cloud, manifest, output):
    total, pages, extras, cursor = None, {}, [], None
    seen = set()
    while True:
        response = await cloud.search(limit=100, cursor=cursor)
        total = response["totalSize"] if total is None else total
        for item in response["results"]:
            doc = str(item["content"]["id"])
            title = item["title"]
            row = manifest.get(title)
            if row is None:
                extras.append({"id": doc, "title": title})
            elif row["xerj_id"] in pages:
                raise ValueError("Duplicate imported title in cloud inventory")
            else:
                pages[row["xerj_id"]] = doc
        cursor = next_cursor(response)
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("Confluence reused a pagination cursor")
        seen.add(cursor)
    result = {"site": cloud.site, "space": cloud.space, "archive_sha256": SHA256, "visible_all": total,
              "expected": len(manifest), "visible_corpus": len(pages), "extra_pages": extras,
              "pages": pages, "gold_reads": {}, "verified": False}
    save(output, result)
    if len(pages) != len(manifest):
        return result
    gold = [row for row in manifest.values() if row["is_gold"].lower() == "true"]
    with ZipFile(ARCHIVE) as archive:
        for pos, row in enumerate(gold, 1):
            original = normalize(archive.read(row["archive_path"]).decode())
            ref = pages[row["xerj_id"]]
            native = await cloud.call("getConfluenceContent", content_id=ref, detail="full", content_format="html")
            parser = VisibleText()
            parser.feed(native["body"]["value"])
            parser.close()
            observed = normalize("".join(parser.parts))
            result["gold_reads"][row["xerj_id"]] = {"page": ref, "text_matches": original == observed,
                "original_chars": len(original), "imported_chars": len(observed),
                "space_id": native.get("spaceId")}
            if pos % 8 == 0:
                print(f"Read {pos}/{len(gold)} cloud references", flush=True)
                save(output, result)
    result["verified"] = all(item["text_matches"] for item in result["gold_reads"].values())
    save(output, result)
    return result


def local_index(index, manifest):
    json_request(f"{XERJ}/{index}", {"mappings": {"properties": {"content": {"type": "text"}}}}, method="PUT")
    batch = []
    with ZipFile(ARCHIVE) as archive:
        for row in manifest.values():
            chunk = archive.read(row["archive_path"]).decode()
            batch.extend((json.dumps({"index":{"_index": index, "_id": row["xerj_id"]}}).encode(),
                          json.dumps({"content":chunk}, ensure_ascii=False).encode()))
            if len(batch) >= 256:
                upload(batch)
                batch.clear()
    if batch:
        upload(batch)
    json_request(f"{XERJ}/{index}/_refresh", {}, method="POST")
    count = json_request(f"{XERJ}/{index}/_count")["count"]
    if count != len(manifest):
        raise ValueError("Local and imported collection counts differ")
    return count


EN_STOP = frozenset("""
a an and any are as at be been being but by can could did do does for from had has have
how i if in into is it its me my no nor not of on or our out over she should so than that
the their them then there these they this those to under up us was we were what when
where which while who why will with would you your
""".split())


def document_frequency(manifest):
    freq = Counter()
    with ZipFile(ARCHIVE) as archive:
        for row in manifest.values():
            content = archive.read(row["archive_path"]).decode().casefold()
            freq.update(set(re.findall(r"[^\W_]+", content)))
    return freq


def selective_terms(query, freq, corpus_size):
    candidates = {}
    for word in re.findall(r"[^\W_]+", query):
        token = word.casefold()
        if token not in EN_STOP and (len(word) >= 3 or word.isupper()) and freq[token] > 0:
            candidates.setdefault(token, None)
    ordered = sorted(candidates, key=lambda token: (freq[token], -len(token)))
    less_common = [word for word in ordered if freq[word] <= corpus_size / 3]
    return " OR ".join((less_common or ordered)[:6]) or cql_terms(query)


async def benchmark(cloud, index, manifest, inventory, db, output, cloud_query="all"):
    entries = inventory["pages"]
    if not inventory["verified"] or inventory["expected"] != len(manifest) or inventory["space"] != cloud.space:
        raise ValueError("The cloud batch must pass inventory and gold-text checks")
    if set(entries) != {row["xerj_id"] for row in manifest.values()}:
        raise ValueError("The manifest differs from the verified cloud batch")
    exclusion = ""
    if inventory["extra_pages"]:
        ids = [item["id"] for item in inventory["extra_pages"]]
        if not all(key.isdigit() for key in ids):
            raise ValueError("Unexpected content ID")
        exclusion = " AND id NOT IN (" + ",".join(ids) + ")"
    reverse = {v: k for k, v in entries.items()}
    release = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    tasks = [row for row in release["rows"] if row["lang"] == "en"]
    local_index(index, manifest)
    frequency = document_frequency(manifest) if cloud_query == "selective" else None
    async with local_session(db, index=index) as local:
        async def query_cloud(text):
            terms = selective_terms(text, frequency, len(manifest)) if frequency is not None else cql_terms(text)
            response = await cloud.search(f'{exclusion} AND text ~ "{terms}"', limit=5)
            return {"ids": [reverse.get(str(item["content"]["id"])) for item in response["results"]],
                    "server_ms": response.get("searchDuration"), "terms": terms,
                    "matches": response["totalSize"]}
        async def query_local(text):
            return {"ids": await local_call(local, "search_corpus", query=text)}
        await query_cloud("gateway timeout readiness")
        await query_local("gateway timeout readiness")
        report = {"source_sha256": SHA256, "size": len(manifest),
                  "utf8_bytes": sum(int(row["utf8_bytes"]) for row in manifest.values()),
                  "transport": {"xerj": "stdio MCP", "confluence": "Atlassian v2 MCP + local OAuth bridge"},
                  "cloud_space": cloud.space, "cloud_query_mode": cloud_query, "rows": []}
        for pos, task in enumerate(tasks):
            calls = {"xerj": query_local, "confluence": query_cloud}
            order = list(calls)[::(-1 if pos % 2 else 1)]
            row = {"id": task["id"], "query": task["query"], "gold": task["gold"], "order": order, "arms": {}}
            for arm in order:
                measured = await timed(lambda: calls[arm](task["query"]))
                ids = measured.get("result", {}).get("ids", [])
                if None in ids:
                    raise ValueError("Confluence search returned a page outside the verified corpus")
                row["arms"][arm] = {**measured, **rank_score(ids, task["gold"])}
            report["rows"].append(row)
            save(output, report)
        report["summary"] = {}
        for arm in calls:
            values = [row["arms"][arm] for row in report["rows"]]
            report["summary"][arm] = {"queries": len(values), "hit5": sum(v["hit5"] for v in values),
                "recall5": statistics.mean(v["recall5"] for v in values),
                "errors": sum("error" in v for v in values), **stats(v["ms"] for v in values)}
        save(output, report)
        return report["summary"]


async def main(args):
    cloud = Atlassian(args.socket, args.site, args.space, "0")
    manifest = read_manifest(args.manifest)
    if args.index:
        inventory = json.loads(args.inventory.read_text())
        return await benchmark(cloud, args.index, manifest, inventory, args.db, args.output, args.cloud_query)
    result = await census(cloud, manifest, args.output)
    return {k: v for k, v in result.items() if k not in {"pages", "gold_reads"}}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--space", required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--inventory", type=Path)
    p.add_argument("--index")
    p.add_argument("--cloud-query", choices=("all", "selective"), default="all")
    p.add_argument("--db", type=Path, default=STATE/"retry-cloud-512/benchmark.sqlite")
    args = p.parse_args()
    if args.index and not args.inventory:
        p.error("--index requires --inventory")
    print(json.dumps(asyncio.run(main(args)), indent=2))
