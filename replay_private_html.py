"""Publish an identical HTML batch as private pages in a named test space via Atlassian MCP."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from zipfile import ZipFile

from atlassian_client import Atlassian
from retry_cloud_search import next_cursor, read_manifest
from scale_xerj import save

TEST_PREFIX = "Wiki Benchmark Private Replay "


async def known_pages(client, manifest):
    names, cursor, seen = {}, None, set()
    while True:
        result = await client.search(limit=100, cursor=cursor)
        for item in result["results"]:
            if item["title"] in manifest:
                names[item["title"]] = str(item["content"]["id"])
        cursor = next_cursor(result)
        if cursor is None:
            return names
        if cursor in seen:
            raise ValueError("Repeated cloud pagination cursor")
        seen.add(cursor)


async def replay(args):
    discovery = Atlassian(args.socket, args.site, args.space, "0")
    entries = (await discovery.call("executeRead", name="listConfluenceSpaces", inputs={"limit": 100}))["results"]
    expected = [x for x in entries if x.get("key") == args.space and x.get("name") == args.expected_name]
    if not args.expected_name.startswith(TEST_PREFIX) or len(expected) != 1:
        raise ValueError("The target does not match the explicitly named test space")
    client = Atlassian(args.socket, args.site, args.space, expected[0]["id"])
    manifest = read_manifest(args.manifest)
    await client.call("executeRead", name="getContentFormatGuide", inputs={"toolName": "createConfluencePage"})
    existing = await known_pages(client, manifest)
    if args.checkpoint.exists():
        state = json.loads(args.checkpoint.read_text())
        if (state["space"], state["space_id"], state["pages"]) != (client.space, client.space_id, len(manifest)):
            raise ValueError("Checkpoint belongs to a different test space")
    else:
        state = {"space": client.space, "space_id": client.space_id, "site": client.site, "pages": len(manifest),
                 "initial_visible": len(existing), "acknowledged": {},
                 "mode": "Atlassian MCP createConfluenceContent, private=True, HTML body"}
    save(args.checkpoint, state)
    if args.stop_after and len(state["acknowledged"]) >= args.stop_after:
        return
    start = time.perf_counter()
    with ZipFile(args.archive) as archive:
        files = {Path(name).name: name for name in archive.namelist()}
        for title, row in manifest.items():
            key = row["xerj_id"]
            if title in existing or key in state["acknowledged"]:
                continue
            data = archive.read(files[title+".html"])
            if hashlib.sha256(data).hexdigest() != row["html_sha256"]:
                raise ValueError("HTML document does not match the manifest")
            html = data.decode()
            body = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
            time_before = time.perf_counter()
            reply = await client.call("createConfluenceContent", parent={"spaceId": client.space_id},
                                      contentType="page", title=title,
                                      body={"format": "html", "value": body}, private=True)
            state["acknowledged"][key] = {"ms": (time.perf_counter()-time_before)*1000}
            payload = reply.get("content", reply)
            if isinstance(payload, dict) and (page_id := payload.get("id") or payload.get("contentId")):
                state["acknowledged"][key]["page"] = str(page_id)
            save(args.checkpoint, state)
            count = len(state["acknowledged"])
            if count == 1 or count % 16 == 0 or count == len(manifest):
                print(f"{count} private pages acknowledged; {time.perf_counter()-start:.1f}s in this invocation",
                      flush=True)
            if args.stop_after and count >= args.stop_after:
                break
    state["last_invocation_s"] = time.perf_counter()-start
    save(args.checkpoint, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--expected-name", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--stop-after", type=int)
    asyncio.run(replay(parser.parse_args()))
