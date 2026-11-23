"""English versioned writing fixture, identical on both platforms."""
import argparse
import asyncio
from html import escape
from pathlib import Path
import re

from atlassian_client import Atlassian
from corpus import STATE
from scale_xerj import save
from wiki import add_page, connect, initialize

SECTIONS = {
    "decision": [("Approved policy", "EU gateway requests must time out after 45 seconds."),
                 ("Applicability", "Only applies to customer-facing EU production.")],
    "guide": [("Gateway timeout", "EU gateway requests currently time out after 90 seconds."),
              ("Escalation", "If measurements disagree with the contract, contact the on-call engineer.")],
    "draft": [("Draft policy", "US gateway requests could time out after 120 seconds."),
              ("Status", "Proposed only. Awaiting approval.")],
}
TITLES = {"decision": "Approved EU decision", "guide": "Gateway operating guide", "draft": "US draft proposal"}


async def seed(wiki, prefix, db, output):
    if not re.fullmatch(r"[a-z][a-z0-9]{3,32}", prefix):
        raise ValueError("Choose a unique lowercase alphanumeric fixture prefix")
    if db.exists() or output.exists():
        raise ValueError("Use a fresh database and manifest")
    async def find():
        results = (await wiki.search(f' AND title ~ "{prefix}"', limit=10))["results"]
        return {h["title"]: h["content"]["id"] for h in results}

    existing = await find()
    if set(existing) - {f"{prefix} {title}" for title in TITLES.values()}:
        raise ValueError("Fixture prefix collides with other pages")
    await wiki.call("executeRead", name="getContentFormatGuide", inputs={"toolName": "createConfluencePage"})
    ids = {}
    for key, sections in SECTIONS.items():
        title = f"{prefix} {TITLES[key]}"
        doc = existing.get(title)
        if not doc:
            body = "".join(f"<h2>{escape(h)}</h2><p>{escape(t)}</p>" for h, t in sections)
            reply = await wiki.call("createConfluenceContent", parent={"spaceId": wiki.space_id},
                                    contentType="page", title=title,
                                    body={"format": "html", "value": body}, private=True)
            doc = reply.get("id") or reply.get("contentId")
            if not doc:
                for _ in range(12):
                    doc = (await find()).get(title)
                    if doc:
                        break
                    await asyncio.sleep(3)
                else:
                    raise RuntimeError(f"Created page did not become searchable: {title}")
        page = await wiki.read(doc)
        if page["metadata"]["version"]["number"] != 1 or any(
            t not in page["body"]["value"] for _, t in sections
        ):
            raise ValueError(f"Cloud fixture differs from the original: {title}")
        ids[key] = str(doc)
        print(key, doc, flush=True)
    initialize(db)
    with connect(db, True) as tx:
        for key, sections in SECTIONS.items():
            add_page(tx, f"{prefix}-{key}", f"{prefix} {TITLES[key]}", "en", sections)
    save(output, {"prefix": prefix, "cloud_ids": ids, "site": wiki.site, "space": wiki.space,
                  "space_id": wiki.space_id, "local_db": str(db.resolve())})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prefix")
    parser.add_argument("--socket", default=STATE/"stages/atlassian.sock")
    parser.add_argument("--site", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(seed(Atlassian(args.socket, args.site, args.space, args.space_id),
                     args.prefix, args.db, args.output))
