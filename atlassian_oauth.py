"""Official Atlassian MCP over OAuth with a private, temporary Unix socket."""
from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP = "https://mcp.atlassian.com/v2/mcp"
ROOT = "https://auth.atlassian.com"
SCOPES = "read:me read:account offline_access read:confluence:agent-interface search:confluence:agent-interface write:confluence:agent-interface"
state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
code = error = None

class Callback(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        global code, error
        url = urlsplit(self.path)
        if url.path == "/complete":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write("Ответ Atlassian получен. Можно вернуться в чат.".encode())
            return
        qs = parse_qs(url.query)
        if url.path != "/callback" or not secrets.compare_digest(qs.get("state", [""])[0], state):
            self.send_error(400)
            return
        code = qs.get("code", [None])[0]
        error = qs.get("error", [None])[0]
        self.send_response(303)
        self.send_header("Location", "/complete")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

class SessionAuth(httpx.Auth):
    """Rotate tokens in process memory only."""
    def __init__(self, credentials, client_id, endpoint):
        self.client_id = client_id
        self.endpoint = endpoint
        self.lock = asyncio.Lock()
        self.refresh = credentials.get("refresh_token")
        self.remember(credentials)

    def remember(self, credentials):
        self.access = credentials["access_token"]
        self.refresh = credentials.get("refresh_token", self.refresh)
        self.deadline = time.monotonic() + int(credentials.get("expires_in", 3600)) - 60

    async def async_auth_flow(self, request):
        async with self.lock:
            if time.monotonic() >= self.deadline and self.refresh:
                async with httpx.AsyncClient(trust_env=False, timeout=30) as auth:
                    reply = await auth.post(self.endpoint, data={
                        "grant_type": "refresh_token", "client_id": self.client_id,
                        "refresh_token": self.refresh})
                    reply.raise_for_status()
                    self.remember(reply.json())
            request.headers["Authorization"] = "Bearer " + self.access
        yield request

async def use(credentials, client_id, endpoint, socket, *, catalog=False):
    auth = SessionAuth(credentials, client_id, endpoint)
    async with httpx.AsyncClient(auth=auth, trust_env=False,timeout=90) as client:
        async with streamable_http_client(MCP, http_client=client) as (read,write,_):
            async with ClientSession(read,write) as session:
                initialized = await session.initialize()
                listing = await session.list_tools()
                tools = list(listing.tools)
                if catalog:
                    while listing.nextCursor:
                        listing = await session.list_tools(cursor=listing.nextCursor)
                        tools.extend(listing.tools)
                supported = {tool.name for tool in tools}
                lock = asyncio.Lock()

                async def handle(reader, writer):
                    try:
                        request = json.loads(await reader.readline())
                        name = request["name"]
                        if catalog and name == "benchmark/tools":
                            response = json.dumps({
                                "structuredContent": {
                                    "protocol": initialized.protocolVersion,
                                    "server": initialized.serverInfo.model_dump(exclude_none=True),
                                    "tools": [tool.model_dump(exclude_none=True) for tool in tools],
                                },
                            })
                        elif name not in supported:
                            raise ValueError("Unknown Atlassian MCP operation")
                        else:
                            async with lock:
                                result = await session.call_tool(name, request["arguments"])
                            response = result.model_dump_json(exclude_none=True)
                    except (KeyError, ValueError, TimeoutError) as exc:
                        response = json.dumps({"isError": True, "structuredContent": {"error": str(exc)}})
                    try:
                        writer.write(response.encode() + b"\n")
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_unix_server(handle, path=socket, limit=2**22)
                socket.chmod(0o600)
                print("Atlassian OAuth connected", flush=True)
                try:
                    async with server:
                        await server.serve_forever()
                finally:
                    socket.unlink(missing_ok=True)


def run(socket, *, read_only=False, rovo=False):
    scopes = SCOPES.removesuffix(" write:confluence:agent-interface") if read_only or rovo else SCOPES
    if rovo:
        scopes += " search:rovo:agent-interface"
    if socket.exists():
        raise ValueError("Choose an unused socket path")
    os.umask(0o077)
    socket.parent.mkdir(parents=True, exist_ok=True)
    with HTTPServer(("127.0.0.1",0), Callback) as server, httpx.Client(trust_env=False,timeout=20) as client:
        callback=f"http://localhost:{server.server_port}/callback"
        prm=client.get("https://mcp.atlassian.com/.well-known/oauth-protected-resource/v2/mcp").raise_for_status().json()
        issuer=urlsplit(prm["authorization_servers"][0])
        if (issuer.scheme, issuer.hostname) != ("https", "auth.atlassian.com"):
            raise ValueError("Unexpected Atlassian OAuth issuer")
        meta=client.get(ROOT+"/.well-known/oauth-authorization-server"+issuer.path).raise_for_status().json()
        endpoints = ("token_endpoint", "registration_endpoint", "authorization_endpoint")
        if any(urlsplit(meta[k]).netloc != "auth.atlassian.com" or urlsplit(meta[k]).scheme != "https" for k in endpoints):
            raise ValueError("Unexpected Atlassian OAuth endpoints")
        reg = client.post(meta["registration_endpoint"], json={
            "client_name": "Local Wiki Benchmark", "redirect_uris": [callback],
            "response_types": ["code"], "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none", "scope": scopes})
        reg.raise_for_status()
        client_id = reg.json()["client_id"]
        challenge=urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = meta["authorization_endpoint"]+"?"+urlencode(dict(client_id=client_id,response_type="code",redirect_uri=callback,resource=MCP,
            audience="api.atlassian.com",scope=scopes,state=state,code_challenge=challenge,code_challenge_method="S256"))
        if not webbrowser.open(url):
            raise RuntimeError("Could not open browser for Atlassian authorization")
        print("Atlassian consent opened in the browser", flush=True)
        deadline=time.monotonic()+3600
        server.timeout=2
        while not code and not error and time.monotonic()<deadline:
            server.handle_request()
        if not code:
            print("Consent did not complete",error or "timeout",flush=True)
            return 1
        server.timeout = 0.5
        server.handle_request()
        res=client.post(meta["token_endpoint"],data=dict(grant_type="authorization_code",client_id=client_id,code=code,code_verifier=verifier,redirect_uri=callback,resource=MCP))
        if res.is_error:
            print("Token exchange failed",res.status_code,res.json().get("error"),flush=True)
            return 1
        credentials=res.json()
    asyncio.run(use(credentials, client_id, meta["token_endpoint"], socket, catalog=rovo))

if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--rovo", action="store_true", help="Read-only Confluence and Rovo Search, with tool catalog")
    args = parser.parse_args()
    try:
        sys.exit(run(args.socket.resolve(), read_only=args.read_only, rovo=args.rovo))
    except KeyboardInterrupt:
        sys.exit(0)
