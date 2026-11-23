"""Reapply successful recorded mutations to clean fixtures and independently rescore answers."""
import argparse
import json
from pathlib import Path
import tempfile
from corpus import STATE
from fixtures import CASES, seed
from run_agent import validate
from wiki import initialize, replace
from site_view import export


def main():
    p = argparse.ArgumentParser()
    p.add_argument("trace",type=Path)
    p.add_argument("output",type=Path)
    p.add_argument("--export",type=Path)
    p.add_argument("--page",help="Only export one page, its history and sources")
    args=p.parse_args()
    runs=json.loads(args.trace.read_text())
    cases={c[0]:c for c in CASES}
    with tempfile.TemporaryDirectory(dir=STATE) as tmp:
        path=Path(tmp)/"wiki.db"
        initialize(path)
        seed(path)
        for r in runs:
            key=r["service"]
            for step in r["writer"]["trace"]:
                if step["tool"]=="update_section" and not step["error"]:
                    replace(path,**step["args"],role="editor")
            r["checks"].update(validate(path,cases[key],r["reader"]["answer"]))
            r["passed"]=all(r["checks"].values())
            print(key,r["passed"],[k for k,v in r["checks"].items() if not v])
        if args.export:
            export(path,args.export,args.page)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(runs,ensure_ascii=False,indent=2)+"\n")


if __name__=="__main__":
    main()
