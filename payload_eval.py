"""Token volume for a retrieved section versus its whole parent page, using the actual Qwen tokenizer."""
import json
import re
from corpus import STATE, questions
from retrieval import json_request
from wiki import DEFAULT_DB, connect, page


def main():
    rows = json.loads((STATE/"fulltext.json").read_text())
    out = []
    with connect(DEFAULT_DB) as db:
        ids = {r[0] for r in db.execute("SELECT id FROM pages WHERE id GLOB 'dsid_*'")}
        qs = {q["question_id"]:q for q in questions(ids)}
        for row in rows:
            if row["lang"] != "en" or not row["hit"]:
                continue
            gold = qs[row["id"]]["expected_doc_ids"]
            for hit in row["hits"]:
                doc,rev,sec = re.fullmatch(r"/p/([\w-]+)/v(\d+)\.html#s(\d+)",hit).groups()
                if doc in gold:
                    break
            result = dict(id=row["id"],hit=hit)
            for mode, selector in [("section",int(sec)),("page",None)]:
                content = json.dumps(page(db,doc,int(rev),selector),ensure_ascii=False)
                result[mode] = len(json_request("http://127.0.0.1:19391/tokenize",
                                       {"content":content,"add_special":False})["tokens"])
            out.append(result)
    (STATE/"payload.json").write_text(json.dumps(out,indent=2)+"\n")
    print(len(out),sum(r["section"] for r in out),sum(r["page"] for r in out))


if __name__=="__main__":
    main()
