"""Read-only localhost browser and portable HTML export for the demo."""
from base64 import b64decode
from difflib import unified_diff
from hmac import compare_digest
from html import escape as esc
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote
import re
from corpus import ROOT
from materials import source_asset
from review import decide, propose, read_draft
from wiki import allowed, connect, history, page, search

CSS = (ROOT/"style.css").read_text()


def layout(title, content, home, lang="ru", attributes="", script=""):
    return (f'<!doctype html><html lang="{esc(lang)}"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)} · Team Wiki</title><style>{CSS}</style>'
            f'<header><div><a href="{home}">Team Wiki</a><span>База знаний</span></div></header>'
            f'<main{attributes}>{content}</main>{script}</html>')


def article(data, *, live=False, asset=None, can_edit=False):
    rev = data["revision"]
    items = []
    for b in data["sections"]:
        link = ""
        if b["source"]:
            doc,v,sec = re.fullmatch(r"([\w-]+)@(\d+)#s(\d+)",b["source"]).groups()
            link = f'<p><small>Основание · <a href="../{doc}/v{v}.html#s{sec}">{esc(b["source"])}</a></small></p>'
        edit = ""
        if can_edit:
            edit = f'<p><a href="/edit/{data["id"]}/{b["sec"]}">Предложить правку</a></p>'
        items.append(f'<article id="s{b["sec"]}"><h2>{esc(b["heading"])}</h2>'
                     f'<div class="body">{esc(b["body"])}</div>{link}{edit}</article>')
    nav = " ".join(f'<a href="#s{b["sec"]}">{esc(b["heading"])}</a>' for b in data["sections"])
    history = f' · <a href="v{rev-1}.html">Предыдущая редакция</a>' if rev>1 else ""
    original = ""
    if asset:
        original = (f'<p class="meta">Импортировано {esc(asset["imported_at"])} · {esc(asset["author"])} · '
                    f'<a href="{asset["url"]}">Оригинал {esc(asset["filename"])}</a></p>')
    controls = ""
    attributes = ""
    script = ""
    if live:
        doc = esc(data["id"])
        controls = (f' · <a href="/p/{doc}/index.html">Текущая</a>'
                    f' · <a href="/p/{doc}/v{rev}.html">Постоянная ссылка</a>'
                    f' · <a href="/history/{doc}">История</a>')
        attributes = f' data-doc="{doc}" data-revision="{rev}" data-revision-url="/p/{doc}/v{rev}.html"'
        script = '<script type="module" src="/wiki-context.js"></script>'
    return layout(data["title"], f'<p class="meta">{esc(data["lang"].upper())} · Редакция {rev}{history}{controls}</p>'
                  f'<h1>{esc(data["title"])}</h1>{original}<nav aria-label="Разделы">{nav}</nav>'+''.join(items),
                  "../../index.html", data["lang"], attributes, script)


def index(db, query=None, static=False, selected=None, *, principal=None, hits=None):
    if query:
        rows = search(db, query, 10, principal=principal) if hits is None else hits
        links = []
        for r in rows:
            if not allowed(db, r["doc"], principal):
                continue
            links.append(f'<li><a href="{esc(r["url"])}">{esc(r["title"])} · '
                         f'{esc(r["heading"])}</a><p>{esc(r["excerpt"])}</p></li>')
    else:
        rows = db.execute("""SELECT * FROM pages p WHERE (? IS NULL OR p.id=?)
            AND (?=0 OR ? IS NOT NULL OR p.id NOT GLOB 'dsid_*')
            AND (NOT EXISTS(SELECT 1 FROM restricted_pages r WHERE r.doc=p.id)
              OR EXISTS(SELECT 1 FROM page_grants g WHERE g.doc=p.id AND g.principal=?))
            ORDER BY (id GLOB 'dsid_*'),title LIMIT 30""", (selected, selected, static, selected, principal))
        links = [f'<li><a href="p/{r["id"]}/v{r["rev"]}.html">{esc(r["title"])}</a> '
                 f'<small>{esc(r["lang"].upper())} · v{r["rev"]}</small></li>' for r in rows]
    form = "" if static else (f'<form action="/"><input type="search" name="q" aria-label="Поиск по статьям" '
                              f'placeholder="Найти статью или раздел" value="{esc(query or "")}">'
                              '<button>Поиск</button></form>')
    return layout("Статьи",'<h1>Статьи команды</h1>'+form+
                  ('<ul class="results">'+''.join(links)+'</ul>' if links else '<p>Совпадений нет.</p>'),"index.html")


def export(path,dest,selected=None):
    dest.mkdir(parents=True,exist_ok=True)
    with connect(path) as db:
        # Self-contained demo export. Use the HTTP view for all 5,189 imported pages.
        pending = list(db.execute("SELECT id,rev FROM pages WHERE id NOT GLOB 'dsid_*' AND id NOT IN (SELECT doc FROM restricted_pages)"))
        if selected:
            revision = page(db,selected)["revision"]
            pending = [(selected,revision)]
        written = set()
        while pending:
            doc, newest = pending.pop()
            folder = dest/"p"/doc
            folder.mkdir(parents=True,exist_ok=True)
            for rev in range(1,newest+1):
                if (doc,rev) in written:
                    continue
                written.add((doc,rev))
                snapshot = page(db,doc,rev)
                (folder/f"v{rev}.html").write_text(article(snapshot))
                for block in snapshot["sections"]:
                    if block["source"]:
                        target, version = block["source"].split("@")
                        pending.append((target,int(version.split("#")[0])))
        (dest/"index.html").write_text(index(db,static=True,selected=selected))


def source_link(source):
    doc, revision, sec = source.replace("#s", "@").split("@")
    return f"/p/{doc}/v{revision}.html#s{sec}"


def review_html(draft):
    buttons = ""
    if draft["can_decide"]:
        buttons = ('<form method="post"><button name="decision" value="approve">Принять</button> '
                   '<button name="decision" value="reject">Отклонить</button></form>')
    states = {"pending": "На проверке", "approved": "Принята", "rejected": "Отклонена"}
    return layout(draft["title"], f'<h1>{esc(draft["title"])}</h1>'
                  f'<p role="status">{states[draft["state"]]}</p><h2>Было</h2><pre>{esc(draft["before"])}</pre>'
                  f'<h2>Предложение</h2><pre>{esc(draft["body"])}</pre>'
                  f'<p><a href="{source_link(draft["source"])}">Открыть основание</a></p>{buttons}', "/")


def build_server(path, port, *, engine="sqlite", identities=None):
    identities = identities or {}
    lexical = None
    if engine == "lexical":
        from lexical_wiki import LexicalSearch
        lexical = LexicalSearch(path)

    class View(BaseHTTPRequestHandler):
        def identity(self):
            kind, _, value = self.headers.get("Authorization", "").partition(" ")
            if kind.lower() == "basic":
                try:
                    username, password = b64decode(value, validate=True).decode().split(":", 1)
                except (ValueError, UnicodeError):
                    return None, "reader"
                record = identities.get(username)
                if record and compare_digest(password.encode(), record["token"].encode()):
                    return username, record["role"]
            return None, "reader"

        def send(self, status, data=b"", content_type="text/html; charset=utf-8", **headers):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Vary", "Authorization")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if content_type.startswith("text/html"):
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                                 "style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self):
            target = urlsplit(self.path)
            principal, role = self.identity()
            if target.path == "/login":
                if principal:
                    return self.send(303, Location="/")
                return self.send(401, **{"WWW-Authenticate": 'Basic realm="Wiki"'})
            if target.path == "/wiki-context.js":
                code = (ROOT/"wiki_context.js").read_bytes()
                return self.send(200, code, "text/javascript; charset=utf-8")
            query = parse_qs(target.query).get("q", [""])[0]
            try:
                hits = lexical.search(query, 10, principal) if lexical and query and target.path in {"/", "/index.html"} else None
                with connect(path) as db:
                    if target.path in {"/", "/index.html"}:
                        html = index(db, query, principal=principal, hits=hits)
                    else:
                        html = self.detail(db, target.path, principal, role)
            except ValueError:
                return self.send(404, b"Page not found")
            except OSError:
                return self.send(503, b"Search temporarily unavailable")
            if isinstance(html, dict):
                name = quote(html["filename"], safe="")
                return self.send(200, html["data"], "application/octet-stream",
                                 **{"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
            self.send(200, html.encode())

        def detail(self, db, route, principal, role):
            if match := re.fullmatch(r"/p/([\w-]+)/(?:v(\d+)|index)\.html", route):
                doc, rev = match.groups()
                data = page(db, doc, int(rev) if rev else None, principal=principal)
                return article(data, live=True, asset=source_asset(db, doc, principal),
                               can_edit=role == "editor" and allowed(db, doc, principal, edit=True))
            if match := re.fullmatch(r"/history/([\w-]+)(?:/(\d+))?", route):
                doc, rev = match.groups()
                versions = history(db, doc, principal)
                snapshot = page(db, doc, principal=principal)
                if rev:
                    newer = page(db, doc, int(rev), principal=principal)
                    older = page(db, doc, int(rev)-1, principal=principal)
                    def lines(x):
                        return "\n\n".join(f'{b["heading"]}\n{b["body"]}' for b in x["sections"]).splitlines()
                    diff = "\n".join(unified_diff(lines(older), lines(newer), lineterm=""))
                    return layout(snapshot["title"], f'<h1>Изменения к редакции {int(rev)}</h1><pre>{esc(diff)}</pre>', "/")
                items = []
                for v in versions:
                    revision = v["revision"]
                    diff = f' · <a href="/history/{doc}/{revision}">Изменения</a>' if revision > 1 else ""
                    events = []
                    for event in v["events"]:
                        evidence = (f' · <a href="{source_link(event["source"])}">Основание</a>'
                                    if event["source"] else "")
                        events.append(f'<p>{esc(event["actor"])} · {esc(event["at"])}{evidence}</p>')
                    items.append(f'<li><a href="/p/{doc}/v{revision}.html">Редакция {revision}</a>{diff}{"".join(events)}</li>')
                return layout(snapshot["title"], f'<h1>История · {esc(snapshot["title"])}</h1><ul>{"".join(items)}</ul>', "/")
            if match := re.fullmatch(r"/review/([0-9a-f]{32})", route):
                return review_html(read_draft(db, match[1], principal, role))
            if match := re.fullmatch(r"/edit/([\w-]+)/(\d+)", route):
                doc, sec = match[1], int(match[2])
                if role != "editor" or not allowed(db, doc, principal, edit=True):
                    raise ValueError("Unknown page")
                snapshot = page(db, doc, section=sec, principal=principal)
                section = snapshot["sections"][0]
                return layout(snapshot["title"], f'<h1>{esc(section["heading"])}</h1><form method="post">'
                              f'<input type="hidden" name="revision" value="{snapshot["revision"]}">'
                              f'<label>Текст<textarea name="body" rows="8" required>{esc(section["body"])}</textarea></label>'
                              '<label>Основание в формате page@revision#sN<input name="source" required></label>'
                              '<p><button>Отправить на проверку</button></p></form>', "/")
            if match := re.fullmatch(r"/assets/([\w-]+)/original", route):
                asset = source_asset(db, match[1], principal, original=True)
                if asset:
                    return asset
            raise ValueError("Unknown route")

        def do_POST(self):
            principal, role = self.identity()
            origin = self.headers.get("Origin", "")
            if role != "editor" or origin != f"http://127.0.0.1:{self.server.server_port}":
                return self.send(403, b"Forbidden")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 50000:
                    raise ValueError("Invalid request size")
                fields = parse_qs(self.rfile.read(size).decode("utf-8"), max_num_fields=8)
                target = urlsplit(self.path).path
                if match := re.fullmatch(r"/review/([0-9a-f]{32})", target):
                    decide(path, match[1], fields["decision"][0], actor=principal, role=role)
                elif match := re.fullmatch(r"/edit/([\w-]+)/(\d+)", target):
                    result = propose(path, doc=match[1], sec=int(match[2]), revision=int(fields["revision"][0]),
                                     body=fields["body"][0], source=fields["source"][0], actor=principal, role=role)
                    target = result["url"]
                else:
                    return self.send(404, b"Page not found")
            except (ValueError, KeyError) as exc:
                return self.send(409, str(exc).encode(), "text/plain; charset=utf-8")
            self.send(303, Location=target)

    return ThreadingHTTPServer(("127.0.0.1", port), View)


def serve(path, port, *, engine="sqlite", identities=None):
    with build_server(path, port, engine=engine, identities=identities) as server:
        server.serve_forever()
