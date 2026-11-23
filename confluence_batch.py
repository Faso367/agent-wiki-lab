"""Build smaller Confluence HTML packages with every frozen English gold document."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import re
from zipfile import ZIP_DEFLATED, ZipFile

from corpus import ROOT, STATE
from pack_confluence import FOLDER
from scale_xerj import SHA256, save

PACKAGE = STATE/"confluence-import"
SEED = 20261122


def prepare(output, count, folder=None):
    if folder is not None and not re.fullmatch(r"[A-Za-z0-9 -]{1,80}", folder):
        raise ValueError("Invalid archive folder")
    output.mkdir(parents=True, exist_ok=True)
    baseline = json.loads((ROOT/"results/full-release-lexical.json").read_text())
    gold = {doc for task in baseline["rows"] if task["lang"] == "en" for doc in task["gold"]}
    with (PACKAGE/"pages.csv").open(newline="") as f:
        wiki = [row for row in csv.DictReader(f) if row["xerj_id"].startswith("confluence:")]
    required = [row for row in wiki if row["xerj_id"] in gold]
    if {row["xerj_id"] for row in required} != gold or not len(required) <= count <= len(wiki):
        raise ValueError(f"Choose between {len(gold)} and {len(wiki)} wiki pages")
    other = [row for row in wiki if row["xerj_id"] not in gold]
    picked = required + random.Random(SEED).sample(other, count-len(required))
    picked.sort(key=lambda row: row["xerj_id"])
    target = output/"corpus.zip"
    manifest = output/"pages.csv"
    if target.exists() or manifest.exists():
        raise FileExistsError("Choose a fresh output directory")
    with ZipFile(PACKAGE/"enterprise-html.zip") as source, ZipFile(
        target, "w", compression=ZIP_DEFLATED, compresslevel=3, allowZip64=False
    ) as dest, manifest.open("w", newline="") as file:
        table = csv.DictWriter(file, fieldnames=[*picked[0], "html_sha256", "is_gold"])
        table.writeheader()
        for row in picked:
            filename = row["page_title"] + ".html"
            data = source.read(f"{FOLDER}/{filename}")
            dest.writestr(f"{folder}/{filename}" if folder else filename, data)
            table.writerow({**row, "html_sha256": hashlib.sha256(data).hexdigest(),
                            "is_gold": row["xerj_id"] in gold})
        dest.comment = source.comment
    with ZipFile(target) as check:
        if check.testzip() is not None or len(set(check.namelist())) != count:
            raise ValueError("Failed ZIP integrity or unique-title check")
    with target.open("rb") as file:
        archive_hash = hashlib.file_digest(file, "sha256").hexdigest()
    result = {"base_sha256": SHA256, "selection_seed": SEED, "scope": "wiki documents",
              "folder": folder, "pages": count, "gold_pages": len(gold), "utf8_bytes": sum(int(r["utf8_bytes"]) for r in picked),
              "zip_bytes": target.stat().st_size, "zip_sha256": archive_hash}
    save(output/"package.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--pages", type=int, required=True)
    p.add_argument("--folder", help="Wrap all HTML files in one top-level folder")
    args = p.parse_args()
    print(json.dumps(prepare(args.output, args.pages, args.folder), indent=2))
