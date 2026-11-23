"""Client for the experiment's private OAuth bridge to the official Atlassian MCP."""
import asyncio
import json
from pathlib import Path
import re


class Atlassian:
    def __init__(self, socket_path, site, space, space_id):
        if not re.fullmatch(r"[a-z0-9-]+\.atlassian\.net", site):
            raise ValueError("Expected an Atlassian Cloud hostname")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", space) or not str(space_id).isdigit():
            raise ValueError("Invalid test space identifiers")
        self.socket = Path(socket_path).resolve()
        self.site = site
        self.space = space
        self.space_id = str(space_id)

    async def envelope(self, operation, arguments):
        async with asyncio.timeout(90):
            reader, writer = await asyncio.open_unix_connection(self.socket, limit=2**23)
            try:
                request = {"name": operation, "arguments": arguments}
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
        return response

    async def call(self, operation, **arguments):
        response = await self.envelope(operation, {"cloudId": self.site, **arguments})
        data = response.get("structuredContent")
        if data is None:
            data = json.loads(response["content"][0]["text"])
        if response.get("isError") or "error" in data or "errors" in data:
            raise RuntimeError(str(data)[:400])
        return data.get("data", data)

    async def search(self, tail="", limit=5, cursor=None):
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self.call("searchConfluence", cql=f'space = "{self.space}" AND type = page{tail}', **params)

    async def read(self, doc, detail="full", *, content_format="markdown"):
        result = await self.call("getConfluenceContent", content_id=str(doc),
                                 detail=detail, content_format=content_format)
        if str(result["spaceId"]) != self.space_id:
            raise ValueError("Page is outside the test space")
        return result

    def url(self, doc, revision=None):
        base = f"https://{self.site}/wiki/spaces/{self.space}/pages/{doc}"
        if revision is None:
            return base
        return f"https://{self.site}/wiki/pages/viewpage.action?pageId={doc}&pageVersion={revision}"
