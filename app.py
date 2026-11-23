import argparse
import json
from pathlib import Path
from corpus import STATE
from fixtures import seed
from site_view import export, serve
from wiki import DEFAULT_DB, import_public, initialize


def main():
    p = argparse.ArgumentParser(description="Local team wiki")
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    subs = p.add_subparsers(dest="action",required=True)
    init = subs.add_parser("init")
    init.add_argument("--public",action="store_true",help="Import EnterpriseRAG-Bench archives")
    web = subs.add_parser("serve")
    web.add_argument("--port",type=int,default=8765)
    web.add_argument("--engine", choices=["lexical", "sqlite"], default="lexical")
    web.add_argument("--identities", type=Path, help="Private JSON map of user to token and role")
    subs.add_parser("rebuild-index")
    static = subs.add_parser("export-demo")
    static.add_argument("--dest",type=Path,default=STATE/"demo-site")
    args = p.parse_args()
    if args.action == "init":
        initialize(args.db)
        seed(args.db)
        if args.public:
            import_public(args.db)
    elif args.action == "serve":
        print(f"http://127.0.0.1:{args.port}/",flush=True)
        initialize(args.db)
        identities = json.loads(args.identities.read_text()) if args.identities else None
        serve(args.db, args.port, engine=args.engine, identities=identities)
    elif args.action == "rebuild-index":
        from lexical_wiki import LexicalSearch
        initialize(args.db)
        print(json.dumps(LexicalSearch(args.db).rebuild()))
    else:
        export(args.db,args.dest)


if __name__ == "__main__":
    main()
