"""Pinned public input and deterministic document views shared by all search engines."""
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
DOWNLOADS = STATE / "downloads"
INPUTS = {
    "confluence_slice_0001.zip": "a79320d9c11b58c904ddc444f4b5c374ea1d9e6e3075974bcaea294c2a7e2eb5",
    "confluence_slice_0002.zip": "ecb4e395f710f0eef8332b1e27182c00223da847945a010c10078b3e0c693024",
    "questions.jsonl": "f9524b9157cd43aae36b99333a124738804306ea6d07f332d49faa6d3d147905",
}


def verify_inputs():
    for name, digest in INPUTS.items():
        if hashlib.sha256((DOWNLOADS / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Invalid checksum: {name}")


def documents():
    for archive in sorted(INPUTS):
        if not archive.endswith(".zip"):
            continue
        with ZipFile(DOWNLOADS / archive) as z:
            for name in sorted(z.namelist()):
                doc = Path(name).name.split("__")[0]
                content = z.read(name).decode("utf-8").strip()
                title, _, body = content.partition("\n")
                yield doc, title.strip(), body.strip()


def questions(ids):
    rows = map(json.loads, (DOWNLOADS / "questions.jsonl").read_text().splitlines())
    return [r for r in rows if r["expected_doc_ids"] and set(r["expected_doc_ids"]) <= ids]


def card(title, body):
    """Fixed visible text for document discovery, equally available to each engine."""
    return " ".join((title + "\n" + body).split())[:440]
