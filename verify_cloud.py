"""Inventory the cloud corpus through MCP and verify gold text against the source archive."""
import argparse
import asyncio
import csv
import json
from pathlib import Path
import re
from zipfile import ZipFile

from atlassian_client import Atlassian
from corpus import ROOT, STATE
from materials import VisibleText
from scale_xerj import ARCHIVE, SHA256, save


def normalized(text):
    return " ".join(re.sub(r"^\s*#{1,6}[ \t]+", "", text, flags=re.M).split())


async def verify(client, manifest, output, ancestor=None):
    with manifest.open(newline="") as handle:
        source = {r["page_title"]: r for r in csv.DictReader(handle)}
    minimum = {"scope": {"site": client.site, "space": client.space, "ancestor": ancestor},
               "archive_sha256": SHA256, "expected_pages": len(source)}
    restriction = f" AND ancestor = {ancestor}" if ancestor is not None else ""
    batch = await client.search(restriction, limit=100)
    result = {**minimum, "reported_pages": batch["totalSize"], "verified": False}
    save(output, result)
    if batch["totalSize"] != len(source):
        return result
    pages = {}
    cursor = None
    while True:
        for hit in batch["results"]:
            title, doc = hit["title"], hit["content"]["id"]
            if title not in source or source[title]["xerj_id"] in pages:
                raise ValueError("Unexpected or duplicate title in cloud inventory")
            pages[source[title]["xerj_id"]] = doc
        following = batch.get("nextCursor") or batch.get("cursor") or batch.get("_links", {}).get("next")
        if not following:
            break
        if following.startswith("/") or following.startswith("https://"):
            from urllib.parse import parse_qs, urlsplit
            following = parse_qs(urlsplit(following).query).get("cursor", [None])[0]
        if not following or following == cursor:
            raise ValueError("Confluence did not return a usable pagination cursor")
        cursor = following
        batch = await client.search(restriction, limit=100, cursor=cursor)
    if len(pages) != len(source):
        raise ValueError("Cloud pagination ended without confirming all pages")
    data = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    gold = {key for row in data["rows"] if row["lang"] == "en" for key in row["gold"]}
    missing, matched = [], []
    with ZipFile(ARCHIVE) as archive:
        for row in source.values():
            key = row["xerj_id"]
            if key not in gold:
                continue
            native = await client.call("getConfluenceContent", content_id=pages[key],
                                       detail="full", content_format="html")
            parser = VisibleText()
            parser.feed(native["body"]["value"])
            cloud_text = "".join(parser.parts)
            input_text = archive.read(row["archive_path"]).decode()
            (matched if normalized(cloud_text) == normalized(input_text) else missing).append(key)
    result.update(pages=pages, gold_verified=matched, gold_mismatch=missing,
                  verified=set(matched) == gold)
    save(output, result)
    return {k: v for k, v in result.items() if k != "pages"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", default=STATE/"stages/atlassian.sock")
    p.add_argument("--site", required=True)
    p.add_argument("--space", required=True)
    p.add_argument("--space-id", required=True)
    p.add_argument("--ancestor", type=int)
    p.add_argument("--manifest", type=Path, default=STATE/"confluence-import/pages.csv")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    client = Atlassian(args.socket, args.site, args.space, args.space_id)
    print(json.dumps(asyncio.run(verify(client, args.manifest, args.output, args.ancestor)), indent=2))
