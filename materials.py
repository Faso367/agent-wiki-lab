"""Import source files into the wiki with an immutable, access-controlled original."""
import argparse
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import time

from wiki import add_page, allowed, connect, initialize, split_sections

MAX_BYTES = 25_000_000
EXTENSIONS = {".txt", ".md", ".html", ".htm", ".pdf"}
KINDS = {"gpt-report", "claude-artifact", "document"}


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style", "template", "noscript"}:
            self.ignored.append(tag)
        if not self.ignored and tag in {"p", "div", "section", "br", "li", "tr"}:
            self.parts.append("\n\n")
        if not self.ignored and re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n## ")

    def handle_endtag(self, tag):
        if self.ignored and self.ignored[-1] == tag:
            self.ignored.pop()
        if not self.ignored and re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(re.sub(r"\s+", " ", data))


def headed_sections(text):
    sections = []
    title = "Overview"
    position = 0
    for match in re.finditer(r"^#{1,6}\s+([^\n]+)\s*$", text, re.M):
        body = text[position:match.start()].strip()
        if body:
            sections.append((title, body))
        title = match[1].strip()[:255]
        position = match.end()
    body = text[position:].strip()
    if body:
        sections.append((title, body))
    return sections


def extract(original, extension):
    if extension == ".pdf":
        result = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                                input=original, capture_output=True, check=True, timeout=60)
        text = result.stdout.decode("utf-8")
        sections = [(f"Page {n}", p.strip()) for n, p in enumerate(text.split("\f"), 1) if p.strip()]
    else:
        text = original.decode("utf-8-sig")
        if extension in {".html", ".htm"}:
            html = VisibleText()
            html.feed(text)
            html.close()
            text = "".join(html.parts)
        sections = headed_sections(text) if extension != ".txt" else split_sections(text.strip())
    if not sections or not any(body.strip() for _, body in sections):
        raise ValueError("There is no searchable text. Scanned PDFs require OCR.")
    return sections


def import_file(path, file, *, doc, title, lang, kind, author, grants=None):
    file = Path(file)
    extension = file.suffix.lower()
    if extension not in EXTENSIONS or kind not in KINDS or lang not in {"en", "ru"}:
        raise ValueError("Unsupported format, material type or language")
    if not title.strip() or len(title) > 255 or not author.strip():
        raise ValueError("Invalid title or author")
    start = time.perf_counter()
    with file.open("rb") as handle:
        original = handle.read(MAX_BYTES+1)
    if len(original) > MAX_BYTES:
        raise ValueError("Material exceeds the 25 MB input limit")
    sections = extract(original, extension)
    sha = hashlib.sha256(original).hexdigest()
    with connect(path, True) as db:
        add_page(db, doc, title, lang, sections, grants=grants)
        db.execute("INSERT INTO assets(doc,filename,kind,sha256,data,author) VALUES(?,?,?,?,?,?)",
                   (doc, file.name, kind, sha, original, author))
        db.execute("INSERT INTO audit(doc,sec,rev,actor,action) VALUES(?,0,1,?,'import')", (doc, author))
    return {"id": doc, "sha256": sha, "sections": len(sections), "bytes": len(original),
            "import_ms": (time.perf_counter()-start)*1000, "url": f"/p/{doc}/v1.html"}


def source_asset(db, doc, principal=None, *, original=False):
    if not allowed(db, doc, principal):
        return None
    columns = "filename,kind,sha256,author,imported_at" + (",data" if original else "")
    asset = db.execute(f"SELECT {columns} FROM assets WHERE doc=?", (doc,)).fetchone()
    if asset is None:
        return None
    result = dict(asset)
    result["url"] = f"/assets/{doc}/original"
    return result


def main():
    import json
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("file", type=Path)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--doc", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--lang", choices=("en", "ru"), default="en")
    p.add_argument("--kind", choices=sorted(KINDS), default="document")
    p.add_argument("--author", required=True)
    p.add_argument("--private", action="store_true")
    p.add_argument("--reader", action="append", default=[])
    args = p.parse_args()
    grants = {name: False for name in args.reader} | {args.author: True} if args.private else None
    initialize(args.db)
    print(json.dumps(import_file(args.db, args.file, doc=args.doc, title=args.title, lang=args.lang,
                                 kind=args.kind, author=args.author, grants=grants), indent=2))


if __name__ == "__main__":
    main()
