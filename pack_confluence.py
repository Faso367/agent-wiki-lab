"""Package pinned EnterpriseRAG-Bench documents as HTML for Confluence's space importer."""
import csv
import hashlib
import html
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from corpus import ROOT, STATE
from fetch_assets import digest
from scale_xerj import ARCHIVE, SHA256

FOLDER = "Wiki Bench Corpus"
OUT = STATE / "confluence-import"


def main():
    if digest(ARCHIVE) != SHA256:
        raise ValueError("Unexpected source archive")
    OUT.mkdir(exist_ok=True)
    notice = (ROOT / "LICENSE-EnterpriseRAG-Bench.txt").read_text()
    with tempfile.TemporaryDirectory(dir=OUT) as stage, ZipFile(ARCHIVE) as src:
        docs = [d for d in src.infolist() if not d.is_dir() and d.filename != "questions.jsonl"]
        keys = [d.filename.split("/", 1)[0] + ":" + Path(d.filename).name.split("__")[0] for d in docs]
        counts = Counter(keys)
        output = Path(stage) / "enterprise-html.zip"
        manifest = Path(stage) / "pages.csv"
        with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=2) as dst, manifest.open("w", newline="") as table:
            writer = csv.writer(table)
            writer.writerow(["xerj_id", "archive_path", "page_title", "utf8_bytes"])
            for d, key in zip(docs, keys):
                if counts[key] > 1:
                    key += ":" + hashlib.sha256(d.filename.encode()).hexdigest()
                raw = src.read(d).decode("utf-8")
                first = raw.partition("\n")[0]
                title = re.sub(r'[^\w .-]', ' ', first)
                title = ' '.join(title.split()).strip(' .')[:70] or 'Document'
                name = title + " - " + key.replace(":", "-")
                writer.writerow([key, d.filename, name, d.file_size])
                body = ''.join("<p>" + html.escape(p).replace("\n","<br>") + "</p>" for p in raw.split("\n\n"))
                page = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>' + body + '</body></html>'
                dst.writestr(FOLDER + "/" + name + ".html", page)
            dst.comment = notice.encode()
        with ZipFile(output) as check:
            if len(check.namelist()) != len(set(check.namelist())) or len(check.namelist()) != len(docs) or check.testzip() is not None:
                raise ValueError("Invalid HTML archive")
        os.replace(manifest, OUT / manifest.name)
        os.replace(output, OUT / output.name)
        print(len(docs), sum(d.file_size for d in docs), (OUT / output.name).stat().st_size, flush=True)

if __name__ == "__main__":
    main()
